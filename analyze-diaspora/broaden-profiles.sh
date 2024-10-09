#!/usr/bin/env bash
set -ex

input_policy_file=${1?param missing - input policy file.}
output_policy_file=${2?param missing - output policy file.}

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
. "$SCRIPT_DIR/helper.sh"

(
  cat "$input_policy_file"
  cat <<EOF
SELECT \`id\`, \`diaspora_handle\`, \`first_name\`, \`last_name\`, \`image_url\`, \`image_url_small\`,
       \`image_url_medium\`, \`searchable\`, \`person_id\`, \`created_at\`, \`updated_at\`, \`full_name\`, \`nsfw\`,
       \`public_details\`
FROM \`profiles\`;

SELECT *
FROM \`tags\`, \`taggings\`
WHERE \`tags\`.\`id\` = \`taggings\`.\`tag_id\`
  AND \`taggings\`.\`taggable_type\` = 'Profile';

SELECT *
FROM \`profiles\`
WHERE \`public_details\` = TRUE;

SELECT *
FROM \`profiles\`, \`people\`
WHERE \`profiles\`.\`person_id\` = \`people\`.\`id\`
  AND \`people\`.\`owner_id\` = _MY_UID;
EOF
) | remove_subsumed >"$output_policy_file"
