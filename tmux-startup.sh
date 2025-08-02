#!/usr/bin/env bash

# Name of your tmux session
SESSION="dse"

# If the session doesn't already exist, create it (detached) with window 0
tmux has-session -t $SESSION 2>/dev/null || \
  tmux new-session -d -s $SESSION \
    -c "$HOME/dse/concolic_driver" \
    -n driver

# Now create the other windows
tmux new-window -t $SESSION:1 -n config   -c "$HOME/dse/examples"
tmux new-window -t $SESSION:2 -n scripts  -c "$HOME/dse/scripts"
tmux new-window -t $SESSION:3 -n logs     -c "$HOME/dse/logs"
tmux new-window -t $SESSION:4 -n diaspora -c "$HOME/dse/diaspora"

# Finally, attach to the session (or switch if already inside tmux)
if [ -z "$TMUX" ]; then
  tmux attach -t $SESSION
else
  tmux switch-client -t $SESSION
fi
