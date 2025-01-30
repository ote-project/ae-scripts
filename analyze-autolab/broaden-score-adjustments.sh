#!/usr/bin/env bash
set -ex

input_policy_file=${1?param missing - input policy file.}
output_policy_file=${2?param missing - output policy file.}

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
. "$SCRIPT_DIR/helper.sh"

(
  cat "$input_policy_file"
  cat <<EOF
SELECT \`score_adjustments\`.*
FROM \`courses\`,
     \`course_user_data\`,
     \`score_adjustments\`
WHERE \`courses\`.\`id\` = \`course_user_data\`.\`course_id\`
  AND \`course_user_data\`.\`user_id\` = _MY_UID
  AND \`course_user_data\`.\`instructor\` = TRUE
  AND (\`score_adjustments\`.\`id\` = \`courses\`.\`late_penalty_id\`
       OR \`score_adjustments\`.\`id\` = \`courses\`.\`version_penalty_id\`);

SELECT \`score_adjustments\`.*
FROM \`courses\`,
     \`course_user_data\`,
     \`score_adjustments\`
WHERE \`courses\`.\`id\` = \`course_user_data\`.\`course_id\`
  AND \`course_user_data\`.\`user_id\` = _MY_UID
  AND \`courses\`.\`disabled\` = FALSE
  AND (\`score_adjustments\`.\`id\` = \`courses\`.\`late_penalty_id\`
       OR \`score_adjustments\`.\`id\` = \`courses\`.\`version_penalty_id\`);
EOF
) | remove_subsumed >"$output_policy_file"
