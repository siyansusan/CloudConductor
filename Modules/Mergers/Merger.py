from GAP_interfaces import Module

class Merger(Module):

    def __init__(self, platform, tool_id, main_module_name=None):
        super(Merger, self).__init__(platform, tool_id)

        self.input_keys  = None
        self.output_keys = None

        # Optionally set name of merger module to the name of the main tool using the merger
        # Conceptually links splitter/tool/merger when being used as if they were a single module
        self.main_module_name = main_module_name if main_module_name is not None else self.__class__.__name__

    def check_init(self):
        cls_name = self.__class__.__name__

        # Generate the set of keys that are required for a class instance
        required_keys = {
            "input_keys": self.input_keys,
            "output_keys": self.output_keys,

            "req_tools":       self.req_tools,
            "req_resources":   self.req_resources,

        }

        # Check if class instance has initialized all the attributes
        for (key_name, attribute) in required_keys.iteritems():
            if attribute is None:
                raise NotImplementedError(
                    "In module %s, the attribute \"%s\" was not initialized!" % (cls_name, key_name))

    def check_input(self, provided_keys):
        return [key for key in self.input_keys if key not in provided_keys]

    def define_output(self):
        return self.output_keys
