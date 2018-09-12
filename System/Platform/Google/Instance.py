import logging
import subprocess as sp
import time
import math

from System.Platform import Process
from System.Platform import Processor
from System.Platform.Google import GoogleCloudHelper

class Instance(Processor):

    def __init__(self, name, nr_cpus, mem, disk_space, **kwargs):
        # Call super constructor
        super(Instance, self).__init__(name, nr_cpus, mem, disk_space, **kwargs)

        # Get required arguments
        self.zone               = kwargs.pop("zone")
        self.service_acct       = kwargs.pop("service_acct")
        self.disk_image         = kwargs.pop("disk_image")

        # Get optional arguments
        self.is_boot_disk_ssd   = kwargs.pop("is_boot_disk_ssd",    False)
        self.nr_local_ssd       = kwargs.pop("nr_local_ssd",        0)

        # Initialize the region of the instance
        self.region             = GoogleCloudHelper.get_region(self.zone)

        # Initialize instance random id
        self.rand_instance_id   = self.name.rsplit("-",1)[-1]

        # Indicates that instance is not resettable
        self.is_preemptible = False

        # Google instance type. Will be set at creation time based on google price scheme
        self.instance_type = None

        # Initialize the price of the run and the total cost of the run
        self.price = 0
        self.cost = 0

        # Flag for whether startup script has completed running
        self.startup_script_complete = False

        # Number of times creation has been reset
        self.creation_resets = 0

    def get_status(self):
        with self.status_lock:
            self.status = self.__sync_status()
            logging.debug("(%s) Status: %s" % (self.name, self.status))
            return self.status

    def adapt_cmd(self, cmd):
        # Adapt command for running on instance through gcloud ssh
        cmd = cmd.replace("'", "'\"'\"'")
        cmd = "gcloud compute ssh gap@%s --command '%s' --zone %s" % (self.name, cmd, self.zone)
        return cmd

    def create(self):

        if self.is_locked():
            logging.error("(%s) Failed to create processor. Processor locked!" % self.name)
            raise RuntimeError("Cannot create processor while locked!")

        # Set status to indicate that commands can't be run on processor because it's busy
        logging.info("(%s) Process 'create' started!" % self.name)
        # Determine instance type and actual resource usage based on current Google prices in instance zone
        self.nr_cpus, self.mem, self.instance_type = GoogleCloudHelper.get_optimal_instance_type(self.nr_cpus,
                                                                                                 self.mem,
                                                                                                 self.zone,
                                                                                                 self.is_preemptible)

        # Determine instance price at time of creation
        self.price = GoogleCloudHelper.get_instance_price(self.nr_cpus,
                                                          self.mem,
                                                          self.disk_space,
                                                          self.instance_type,
                                                          self.zone,
                                                          self.is_preemptible,
                                                          self.is_boot_disk_ssd,
                                                          self.nr_local_ssd)
        logging.debug("(%s) Instance type is %s. Price per hour: %s cents" % (self.name, self.instance_type, self.price))

        # Generate gcloud create cmd
        cmd = self.__get_gcloud_create_cmd()

        # Try to create instance until either it's successful, we're out of retries, or the processor is locked
        self.processes["create"] = Process(cmd,
                                           cmd=cmd,
                                           stdout=sp.PIPE,
                                           stderr=sp.PIPE,
                                           shell=True,
                                           num_retries=self.default_num_cmd_retries)
        self.wait_process("create")

        # Wait for startup script to completely finish
        logging.debug("(%s) Waiting for instance startup-script completion..." % self.name)
        self.startup_script_complete = False
        self.wait_until_ready()
        logging.debug("(%s) Instance startup complete! %s Now live and ready to run commands!" % (self.name, self.name))

    def destroy(self, wait=True):

        # Set status to indicate that instance cannot run commands and is destroying
        logging.info("(%s) Process 'destroy' started!" % self.name)
        cmd = self.__get_gcloud_destroy_cmd()

        # Run command, wait for destroy to complete, and set status to 'OFF'
        self.processes["destroy"] = Process(cmd,
                                            cmd=cmd,
                                            stdout=sp.PIPE,
                                            stderr=sp.PIPE,
                                            shell=True,
                                            num_retries=self.default_num_cmd_retries)

        # Wait for delete to complete if requested
        if wait:
            self.wait_process("destroy")

    def wait_process(self, proc_name):
        # Get process from process list
        proc_obj = self.processes[proc_name]

        # Return immediately if process has already been set to complete
        if proc_obj.is_complete():
            return proc_obj.get_output()

        # Wait for process to finish
        out, err = proc_obj.communicate()

        # Set process to complete
        proc_obj.set_complete()

        # Store process output for later use
        proc_obj.set_output(out=out, err=err)

        # Case: Process completed with errors
        if proc_obj.has_failed():
            # Determine whether to retry or raise errors
            self.handle_failure(proc_name, proc_obj)
            # If no errors thrown, try waiting on the process again
            return self.wait_process(proc_name)

        if proc_name == "create":
            # Set start time
            self.set_start_time()

        # Set status to 'OFF' if destroy is True
        elif proc_name == "destroy":
            # Set the stop time
            self.set_stop_time()

        # Case: Process completed
        if proc_obj.do_log_success():
            logging.info("(%s) Process '%s' complete!" % (self.name, proc_name))

        return out, err

    def handle_failure(self, proc_name, proc_obj):

        # Determine if command can be retried
        can_retry = False

        # Raise error if processor is locked
        if self.is_locked() and proc_name != "destroy":
            self.raise_error(proc_name, proc_obj)

        curr_status = self.get_status()

        if curr_status == Processor.OFF:
            if proc_name == "destroy":
                return
            can_retry = proc_name == "create" and proc_obj.get_num_retries() > 0

        elif curr_status == Processor.CREATING:
            can_retry = proc_name == "destroy" and proc_obj.get_num_retries() > 0

        elif curr_status == Processor.AVAILABLE:
            can_retry = proc_obj.get_num_retries() > 0 and proc_name != "create"

        elif curr_status == Processor.DESTROYING:
            can_retry = proc_name == "destroy" and proc_obj.get_num_retries() > 0

        # Retry start/destroy command
        if can_retry and proc_name in ["create", "destroy"]:
            time.sleep(3)
            logging.warning("(%s) Process '%s' failed but we still got %s retries left. Re-running command!" % (self.name, proc_name, proc_obj.get_num_retries()))
            self.processes[proc_name] = Process(proc_obj.get_command(),
                                                cmd=proc_obj.get_command(),
                                                stdout=sp.PIPE,
                                                stderr=sp.PIPE,
                                                shell=True,
                                                num_retries=proc_obj.get_num_retries() - 1)
        # Retry 'run' command
        elif can_retry:
            time.sleep(3)
            logging.warning("(%s) Process '%s' failed but we still got %s retries left. Re-running command!" % (
            self.name, proc_name, proc_obj.get_num_retries()))
            self.run(job_name=proc_name,
                     cmd=proc_obj.get_command(),
                     num_retries=proc_obj.get_num_retries() - 1,
                     docker_image=proc_obj.get_docker_image(),
                     quiet_failure=proc_obj.is_quiet())

        # Raise error if no restarts left
        self.raise_error(proc_name, proc_obj)

    def wait_until_ready(self):
        # Wait until startup-script has completed on instance
        # This signifies that the instance has initialized ssh and the instance environment is finalized
        cycle_count = 1
        # Waiting for 10 minutes for instance metadata to be set to READY
        while cycle_count < 60 and not self.startup_script_complete and not self.is_locked():
            time.sleep(10)
            cycle_count += 1
            self.startup_script_complete = self.poll_startup_script()

        # Throw error if instance locking caused loop to exit
        if self.is_locked():
            logging.debug("(%s) Instance locked while waiting for creation!" % self.name)
            raise RuntimeError("(%s) Instance locked while waiting for creation!" % self.name)

        # Reset if instance not initialized within the alloted timeframe
        elif not self.startup_script_complete:

            # Try creating again if there are still resets
            if self.creation_resets < self.default_num_cmd_retries:
                logging.debug("(%s) Create took more than 10 minutes! Resetting instance!" % self.name)
                self.creation_resets += 1
                self.destroy()
                self.create()

            # Throw error if instance still isn't ready after multiple tries
            else:
                logging.debug("(%s) Instance successfully created but "
                              "never became available after %s resets!" %
                              (self.name, self.default_num_cmd_retries))

                raise RuntimeError("(%s) Instance successfully created but never"
                                   " became available after multiple tries!" %
                                   self.name)

    def raise_error(self, proc_name, proc_obj):
        # Log failure to debug logger if quiet failure
        stdout_msg, stderr_msg = proc_obj.get_output()
        if proc_obj.is_quiet():
            logging.debug("(%s) Process '%s' failed!" % (self.name, proc_name))
            if stdout_msg != "" or stderr_msg != "":
                logging.debug("(%s) The following error was received:\n%s\n%s" % (self.name, stdout_msg, stderr_msg))

        # Warn that process has failed due to cancellation
        elif proc_obj.is_stopped():
            logging.warning("(%s) Process '%s' failed due to cancellation!" % (self.name, proc_name))

        # Log failure to error logger otherwise
        else:
            logging.error("(%s) Process '%s' failed!" % (self.name, proc_name))
            if stdout_msg != "" or stderr_msg != "":
                logging.debug("(%s) The following error was received:\n%s\n%s" % (self.name, stdout_msg, stderr_msg))
        raise RuntimeError("Instance %s has failed!" % self.name)

    def __sync_status(self):
        # Try to return current instance status
        retries = self.default_num_cmd_retries
        while True:
            try:
                return self.__poll_status()
            except BaseException, e:
                if retries == 0:
                    logging.error("(%s) Unable to get instance status!")
                    if e.message != "":
                        logging.error("Received the following error:\n%s" % e.message)
                    raise
                retries -= 1
                time.sleep(5)

    def __poll_status(self):

        if not GoogleCloudHelper.instance_exists(self.name):
            self.startup_script_complete = False
            return Processor.OFF

        # Try to get instance status
        status = GoogleCloudHelper.get_instance_status(self.name, self.zone)
        if status in ["TERMINATED", "STOPPING"]:
            self.startup_script_complete = False
            return Processor.DESTROYING

        elif status in ["PROVISIONING", "STAGING"]:
            self.startup_script_complete = False
            return Processor.CREATING

        elif status == "RUNNING":
            if self.startup_script_complete or self.poll_startup_script():
                self.startup_script_complete = True
                return Processor.AVAILABLE
            self.startup_script_complete = False
            return Processor.CREATING

    def poll_startup_script(self):
        # Return true if instance is currently available for running commands
        try:
            data = GoogleCloudHelper.describe(self.name, self.zone)

        # Catch error related to instance not existing
        except RuntimeError:
            logging.error("(%s) Cannot poll startup script! Instance either was removed or was never created!" % self.name)
            logging.error("(%s) Current instance status: %s" % (self.name, self.get_status()))
            raise

        # Check to see if "READY" has been added to instance metadata indicating startup-script has complete
        for item in data["metadata"]["items"]:
            if item["key"] == "READY":
                return True
        return False

    def __get_gcloud_create_cmd(self):
        # Create base command
        args = list()
        args.append("gcloud compute instances create %s" % self.name)

        # Specify the zone where instance will exits
        args.append("--zone")
        args.append(self.zone)

        # Specify that instance is not preemptible
        if self.is_preemptible:
            args.append("--preemptible")

        # Specify boot disk image
        args.append("--image")
        args.append(str(self.disk_image))

        # Set boot disk size
        args.append("--boot-disk-size")
        if self.disk_space >= 10240:
            args.append("%dTB" % int(math.ceil(self.disk_space / 1024.0)))
        else:
            args.append("%dGB" % int(self.disk_space))

        # Set boot disk type
        args.append("--boot-disk-type")
        if self.is_boot_disk_ssd:
            args.append("pd-ssd")
        else:
            args.append("pd-standard")

        # Add local ssds if necessary
        args.extend(["--local-ssd interface=scsi" for _ in xrange(self.nr_local_ssd)])

        # Specify google cloud access scopes
        args.append("--scopes")
        args.append("cloud-platform")

        # Specify google cloud service account
        args.append("--service-account")
        args.append(str(self.service_acct))

        # Determine Google Instance type and insert into gcloud command
        if "custom" in self.instance_type:
            args.append("--custom-cpu")
            args.append(str(self.nr_cpus))

            args.append("--custom-memory")
            args.append("%sGB" % str(int(self.mem)))
        else:
            args.append("--machine-type")
            args.append(self.instance_type)

        # Add metadata to run base Google startup-script
        startup_script_location = "System/Platform/Google/GoogleStartupScript.sh"
        args.append("--metadata-from-file")
        args.append("startup-script=%s" % startup_script_location)
        return " ".join(args)

    def __get_gcloud_destroy_cmd(self):
        args = list()
        args.append("gcloud compute instances delete %s" % self.name)

        # Specify the zone where instance is running
        args.append("--zone")
        args.append(self.zone)

        # Provide input to the command
        args[0:0] = ["yes", "2>/dev/null", "|"]
        return " ".join(args)
