#!/usr/bin/env python2.7

from GAP_system import Config, TaskManager, Task

# Generating the config object
config = Config("GAP.config")

from GAP_modules import FASTQSplitter as Splitter
from GAP_modules import SamtoolsSamToBam as ConverterSamToBam
from GAP_modules import SamtoolsBAMMerge as BAMMerger

if config.cluster.ID == 0:
    from GAP_modules import SLURM as Cluster

if config.aligner.ID == 0:
    from GAP_modules import BwaAligner as Aligner

splitter    = Splitter(config)
converter   = ConverterSamToBam(config)
merger      = BAMMerger(config)
cluster     = Cluster(config)
aligner     = Aligner(config)
task_manager = TaskManager(config, cluster)

split_count = 0
splitted = config.cluster.nodes > 1

if splitted:
    splitter.byNrReads(config.paths.R1, "PE_R1", int(1e7))
    split_count = splitter.byNrReads(config.paths.R2, "PE_R2", int(1e7))

if config.general.goal == "align":
    if splitted:
        for split_id in range(1, split_count+1):
            task = Task("align_%d" % split_id, config, cluster)
            task.type       ="align"

            task.nodes      = 1
            task.mincpus    = config.cluster.mincpus

            aligner.R1      = "%s/split_R1_%d.fastq" % (config.general.temp_dir, split_id)
            aligner.R2      = "%s/split_R2_%d.fastq" % (config.general.temp_dir, split_id)
            aligner.threads = task.mincpus

            converter.threads = task.mincpus
            if aligner.to_stdout:
                if aligner.output_type == "sam":
                    task.command = "%s | %s > %s/split_%d.bam" % (aligner.getCommand(), converter.getCommand(), config.general.temp_dir, split_id)
                else:
                    task.command = "%s > %s/split_%d.bam" % (aligner.getCommand(), config.general.temp_dir, split_id)
            else:
                task.command = aligner.getCommand()

            task_manager.addTask(task)
    else:
        task = Task("align", config, cluster)
        task.type     = "align"
        
        task.nodes    = 1
        task.mincpus  = config.cluster.mincpus
        
        aligner.R1      = config.paths.R1
        aligner.R2      = config.paths.R2
        aligner.threads = task.mincpus

        converter.threads = task.mincpus
        if aligner.to_stdout:
            if aligner.output_type == "sam":
                task.command = "%s | %s > %s/out.bam" % (aligner.getCommand(), converter.getCommand(), config.general.output_dir)
            else:
                task.command = "%s > %s/out.bam" % (aligner.getCommand(), config.general.output_dir)
        else:
            task.command = aligner.getCommand()

        task_manager.addTask(task)

if splitted:
    task = Task("merge", config, cluster)
    task.type       = "merge"

    task.nodes      = 1
    task.mincpus    = config.cluster.mincpus
    task.requires   = ["align_%d" % split_id for split_id in range(1, split_count+1)]

    merger.nr_splits= split_count
    task.command    = merger.getCommand()

    task_manager.addTask(task)

task_manager.run()
