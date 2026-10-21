<?php
// Applies WordPress' own display filters to exported text, using wp-includes/formatting.php and shortcodes.php from a
// clean WordPress distribution (path in $argv[1]), never code from the compromised installation. The filter chains
// follow wp-includes/default-filters.php for the_content, the_title and comment_text.
// stdin: one {"ID": .., "kind": "content"|"title"|"comment", "text": ..} per line
// stdout: one {"ID": .., "kind": .., "html": ..} per line

function apply_filters($hook, $value, ...$args) { return $value; }
function _x($text, $context, $domain = 'default') { return $text; }
function __($text, $domain = 'default') { return $text; }
function _deprecated_argument(...$args) {}
function _doing_it_wrong(...$args) {}
function get_option($name, $default = false) { return $name === 'use_smilies' ? true : $default; }
function wp_cache_get(...$args) { return false; }
function wp_cache_set(...$args) { return true; }
function is_admin() { return false; }
function current_filter() { return $GLOBALS['current_filter']; }
function includes_url($path = '') { return '/wp-includes/' . $path; }
function site_url() { return 'https://www.javachannel.org'; }
function wp_allowed_protocols() { return ['http', 'https', 'ftp', 'mailto', 'irc', 'ircs', 'news']; }
function wp_parse_url($url, $component = -1) { return parse_url($url, $component); }
// The data is valid UTF-8 already (it came out of MariaDB as JSON); makes wp_check_invalid_utf8() a no-op
function is_utf8_charset() { return false; }
function wp_is_internal_link($url) { return (bool) preg_match('~^https?://(www\.)?javachannel\.org(/|$)~i', $url); }
define('ABSPATH', $argv[1] . '/');
define('WPINC', 'wp-includes');
require $argv[1] . '/wp-includes/formatting.php';
require $argv[1] . '/wp-includes/shortcodes.php';
require $argv[1] . '/wp-includes/kses.php';

// smilies_init() lives in functions.php, which pulls in most of WordPress; load just that function from it.
$functions = file_get_contents($argv[1] . '/wp-includes/functions.php');
preg_match('/^function smilies_init\(\) \{.*?^\}/ms', $functions, $m);
eval($m[0]);
smilies_init();

// Register the shortcodes that occur in the content so shortcode_unautop() unwraps them like WordPress did.
// They are replaced by convert.py, so do_shortcode() is not run.
$shortcode_tags = ['audio' => '__return_empty', 'fep_submission_form' => '__return_empty'];

while (($line = fgets(STDIN)) !== false) {
    $row = json_decode($line, true);
    $text = $row['text'];
    switch ($row['kind']) {
        case 'title':
            $html = trim(convert_chars(wptexturize($text)));
            break;
        case 'comment':
            $current_filter = 'comment_text';
            $html = capital_P_dangit(wpautop(convert_smilies(make_clickable(convert_chars(wptexturize($text))))));
            break;
        default:
            $current_filter = 'the_content';
            if (str_contains($text, '<!-- wp:')) {
                // Block editor content: do_blocks() renders the block comments to nothing, and wpautop is skipped.
                $html = preg_replace('/<!-- \/?wp:.*?-->\n?/s', '', $text);
                $html = wptexturize($html);
            } else {
                $html = shortcode_unautop(wpautop(wptexturize($text)));
            }
            $html = convert_smilies(capital_P_dangit($html));
    }
    echo json_encode(['ID' => $row['ID'], 'kind' => $row['kind'], 'html' => $html],
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE), "\n";
}
