"""Start the local dashboard when Python runs this package as a command.

python -m projectclarity.dashboard delegates to server.main(), which parses the
port and output-directory options and serves the browser interface. This small
wrapper turns the returned status into the process exit code. It does not define
the HTTP routes or research workflow, and importing it does not start the server.
"""
from projectclarity.dashboard.server import main

if __name__ == '__main__':
    main()
