#!/usr/bin/env bash
# One-off, read-only export of the WordPress database and uploads from the (compromised, offline) javachannel VM.
# Produces migration/wp-export/*.jsonl (one JSON object per row) and migration/uploads/.
set -euo pipefail
cd "$(dirname "$0")"
HOST=${HOST:-javachannel.local.yawk.at}
OUT=wp-export
mkdir -p "$OUT"

query() {
  ssh -o BatchMode=yes "$HOST" 'sudo -n mariadb --batch --raw --skip-column-names wordpress' 2>/dev/null
}

echo "SELECT JSON_OBJECT('ID', ID, 'post_author', post_author, 'post_date', post_date, 'post_date_gmt', post_date_gmt,
  'post_modified_gmt', post_modified_gmt, 'post_title', post_title, 'post_name', post_name, 'post_type', post_type,
  'post_status', post_status, 'post_parent', post_parent, 'menu_order', menu_order, 'post_content', post_content,
  'post_excerpt', post_excerpt, 'guid', guid)
  FROM wp_posts WHERE post_type IN ('post','page','nav_menu_item') AND post_status = 'publish' ORDER BY ID;" \
  | query > "$OUT/posts.jsonl"

echo "SELECT JSON_OBJECT('post_id', m.post_id, 'meta_key', m.meta_key, 'meta_value', m.meta_value)
  FROM wp_postmeta m JOIN wp_posts p ON p.ID = m.post_id
  WHERE p.post_status = 'publish' AND m.meta_key IN ('_wp_old_slug','enclosure','_wpcom_is_markdown',
    '_menu_item_type','_menu_item_menu_item_parent','_menu_item_object_id','_menu_item_object','_menu_item_url')
  ORDER BY m.meta_id;" \
  | query > "$OUT/postmeta.jsonl"

echo "SELECT JSON_OBJECT('ID', ID, 'user_nicename', user_nicename,
  'display_name', display_name, 'user_url', user_url) FROM wp_users WHERE ID IN (SELECT post_author FROM wp_posts WHERE post_type = 'post' AND post_status = 'publish') ORDER BY ID;" \
  | query > "$OUT/users.jsonl"

echo "SELECT JSON_OBJECT('object_id', r.object_id, 'taxonomy', x.taxonomy, 'name', t.name, 'slug', t.slug,
  'term_id', t.term_id, 'parent', x.parent, 'description', x.description)
  FROM wp_term_relationships r JOIN wp_term_taxonomy x USING (term_taxonomy_id) JOIN wp_terms t USING (term_id)
  ORDER BY r.object_id, r.term_order, t.name;" \
  | query > "$OUT/terms.jsonl"

echo "SELECT JSON_OBJECT('comment_ID', comment_ID, 'comment_post_ID', comment_post_ID, 'comment_author', comment_author,
  'comment_author_url', comment_author_url, 'comment_date_gmt', comment_date_gmt, 'comment_content', comment_content,
  'comment_parent', comment_parent, 'comment_type', comment_type)
  FROM wp_comments WHERE comment_approved = '1' ORDER BY comment_ID;" \
  | query > "$OUT/comments.jsonl"

echo "SELECT JSON_OBJECT('option_name', option_name, 'option_value', option_value) FROM wp_options
  WHERE option_name IN ('blogname','blogdescription','permalink_structure','gmt_offset','timezone_string','home');" \
  | query > "$OUT/options.jsonl"

[ -n "${SKIP_UPLOADS:-}" ] || {
rm -rf uploads
ssh -o BatchMode=yes "$HOST" 'sudo -n tar -C /var/lib/wordpress/javachannel -c uploads' | tar x
}
wc -l "$OUT"/*.jsonl
