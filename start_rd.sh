#!/bin/bash
# AUTO-RECOVERY: Kill any OTHER zombie processes
# Using a specific pattern that avoids matching the shell execution of this script
PGREP_PIDS=$(pgrep -f "python.*src/main.py.*env.rd")
for pid in $PGREP_PIDS; do
    if [ "$pid" != "$$" ]; then
        kill -9 "$pid" 2>/dev/null
    fi
done

screen -wipe
cd /home/ubuntu/reversebot
source venv/bin/activate
export PYTHONPATH=$PYTHONPATH:.
exec ./venv/bin/python src/main.py --env .env.rd
