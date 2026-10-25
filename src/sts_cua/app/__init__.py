"""Local control app: an HTTP server (stdlib only) that starts and stops the stack, runs the
agent, and shows live runs, results, spend and memory. The API is docs/app-api.md.

- server.py: HTTP handling, token auth, static page, `python -m sts_cua.app`
- routes.py: the API endpoints
- procs.py: the processes it starts (pid files) and finds (pgrep)
- files.py: tailing logs and reading run directories
- static/: the page served at /
"""
