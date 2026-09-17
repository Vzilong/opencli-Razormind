#!/bin/sh
exec /usr/bin/python3 "$(dirname "$0")/runner.py" omp "$@"
