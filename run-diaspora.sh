#!/usr/bin/env bash
set -ex

suffix=${1?param missing - suffix.}

cd "$HOME/dse"
(cd examples; git pull --ff-only)

cd "concolic_driver"
sbt -mem 20480 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/diaspora_comments_index.conf $HOME/dse/logs/diaspora-comments-index-2r-$suffix --execution-logging=inputs-only"
sbt -mem 20480 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/diaspora_conversations_index.conf $HOME/dse/logs/diaspora-conversations-index-2r-$suffix --execution-logging=inputs-only"
sbt -mem 20480 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/diaspora_notifications_index.conf $HOME/dse/logs/diaspora-notifications-index-2r-$suffix --execution-logging=inputs-only"
sbt -mem 20480 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/diaspora_people_show.conf $HOME/dse/logs/diaspora-people-show-2r-$suffix --execution-logging=inputs-only"
sbt -mem 20480 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/diaspora_posts_show.conf $HOME/dse/logs/diaspora-posts-show-2r-$suffix --execution-logging=inputs-only"
sbt -mem 20480 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/diaspora_people_stream.conf $HOME/dse/logs/diaspora-people-stream-2r-$suffix --execution-logging=inputs-only"

