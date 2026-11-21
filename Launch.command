#!/bin/zsh
cd -- "${0:A:h}"
exec "${VALTZ_DIRECTOR_PYTHON:-/usr/bin/python3}" server.py "$@"
