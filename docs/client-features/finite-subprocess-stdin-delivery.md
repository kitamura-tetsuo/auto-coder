# Finite subprocess stdin delivery

`CommandExecutor.run_command()` accepts optional finite text input and delivers
its exact UTF-8 bytes followed by EOF through a private pipe. A managed writer
runs concurrently with stdout and stderr readers so large input and output
cannot deadlock each other, while timeouts and callback interruption retain
control of process cleanup. Input preparation and incomplete delivery fail the
command explicitly, and payload input is rejected for pseudo-terminal runs so
the existing interactive terminal contract remains unambiguous.
