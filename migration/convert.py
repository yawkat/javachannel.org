#!/usr/bin/env python3
"""One-off conversion of the WordPress export (migration/wp-export, see export.sh) into Roq content.

Usage: PHP=/path/to/php WORDPRESS=/path/to/clean/wordpress python3 migration/convert.py [--force]

WORDPRESS must be a clean WordPress distribution (e.g. nixpkgs#wordpress share/wordpress), not the compromised install:
its wpautop() is used to render classic editor content exactly like WordPress did.

The files written are recorded with their hashes in migration/generated.json. A re-run only deletes and overwrites
those, so content added after the migration is left alone, and it stops if one of them was edited since (unless
--force is given, which discards those edits).
"""
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

MIGRATION = Path(__file__).resolve().parent
ROOT = MIGRATION.parent
EXPORT = MIGRATION / 'wp-export'
CONTENT = ROOT / 'content'
DATA = ROOT / 'data'
PUBLIC = ROOT / 'public'
MEDIA = ROOT / 'media'
PAGES_DIR = ROOT / 'pages'
MANIFEST = MIGRATION / 'generated.json'

# Cloudflare Pages refuses files above 25 MiB; bigger uploads are served from GitHub instead.
MAX_PAGES_FILE = 25 * 1024 * 1024
RAW_BASE = 'https://raw.githubusercontent.com/yawkat/javachannel.org/main/media'

# WordPress' default "Sample Page" was never meant to be published content.
SKIPPED_PAGES = {'sample-page'}


def load(name):
    with open(EXPORT / f'{name}.jsonl', encoding='utf-8') as f:
        return [json.loads(line) for line in f]


def wp_filters(rows):
    """Run WordPress' own display filters (migration/autop.php) over (kind, ID, text) rows, kind being "content",
    "title" or "comment". Returns {(kind, ID): html}."""
    stdin = ''.join(json.dumps({'ID': i, 'kind': k, 'text': t}) + '\n' for k, i, t in rows)
    proc = subprocess.run([os.environ.get('PHP', 'php'), str(MIGRATION / 'autop.php'), os.environ['WORDPRESS']],
                          input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f'autop.php failed: {proc.stdout[-2000:]}{proc.stderr[-2000:]}')
    out = proc.stdout
    result = {(r['kind'], r['ID']): unentity(r['html']) for r in map(json.loads, out.splitlines())}
    if len(result) != len(rows):
        raise RuntimeError(f'autop.php returned {len(result)} of {len(rows)} rows')
    return result


def unentity(text):
    """wptexturize writes typographic characters as numeric entities; store them as plain characters instead."""
    text = re.sub(r'&#(8211|8212|8216|8217|8220|8221|8230|215|8242|8243);', lambda m: chr(int(m.group(1))), text)
    return text.replace('&#038;', '&amp;')


def site_relative(text, prefix=r'(?:href|src)=["\']'):
    """Absolute links to the old site become root-relative: http://javachannel.org/posts/x/ becomes /posts/x/, and
    the bare site URL becomes / (an empty href would resolve against the theme's <base href> to the current page).
    By default only href/src attributes are rewritten, not URLs shown as text."""
    return re.sub(f'({prefix})' + r'https?://(?:www\.)?javachannel\.org(?=[/"\'#?]|$)',
                  lambda m: m.group(1) + ('' if text[m.end():m.end() + 1] == '/' else '/'), text, flags=re.I)


def fix_code_block(match):
    """<pre lang="java"> was rendered by the WP-Syntax plugin, which escaped its content. Some of these blocks were
    later mangled by the visual editor, which took generics for HTML tags (List<Long> became List<long>...</long>).
    Restore them as plain escaped code."""
    code = match.group(1)
    if '<' in code:
        code = re.sub(r'</?code>', '', code)
        code = re.sub(r'</[a-z]+>', '', code)
        code = html.escape(code, quote=False)
        code = re.sub(r'&lt;(long|integer|container)&gt;', lambda m: f'&lt;{m.group(1).capitalize()}&gt;', code)
    return f'<pre><code class="language-java">{code}</code></pre>'


def audio_html(url):
    name = url.rsplit('/', 1)[-1]
    return (f'<figure class="podcast-audio"><audio controls preload="none" src="{url}"></audio>'
            f'<figcaption><a href="{url}">Download {name}</a></figcaption></figure>')


def convert_body(body):
    body = re.sub(r'\[audio mp3="([^"]+)"\]\[/audio\]', lambda m: audio_html(m.group(1)), body)
    body = site_relative(body)
    body = re.sub(r'<pre lang="java">(.*?)</pre>', fix_code_block, body, flags=re.S)
    body = body.replace(
        '[fep_submission_form]',
        '<p>The submission form of the old site is gone. To submit content, talk to us in '
        '<a href="https://libera.chat/">#java on Libera.Chat</a> or open a pull request against '
        '<a href="https://github.com/yawkat/javachannel.org">the site\'s repository</a>.</p>')
    # Qute (with alt-expr-syntax) would interpret these sequences as template syntax, e.g. Java annotations in
    # {@JsonSubTypes...}. An entity renders identically.
    body = re.sub(r'\{(?=[=#!/|@])', '&#123;', body)
    return body


def wp_date(local, gmt):
    """The post's local time as WordPress showed it, with its UTC offset (the site used America/New_York until it
    was switched to UTC)."""
    minutes = int((datetime.fromisoformat(local) - datetime.fromisoformat(gmt)).total_seconds()) // 60
    sign = '-' if minutes < 0 else '+'
    return f'{local} {sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}'


def yaml_str(s):
    return json.dumps(s, ensure_ascii=False)


def front_matter(fields):
    lines = ['---']
    for key, value in fields.items():
        if value is None or value == []:
            continue
        if isinstance(value, list):
            lines.append(f'{key}:')
            for item in value:
                if isinstance(item, dict):
                    first = True
                    for k, v in item.items():
                        lines.append(f'  {"- " if first else "  "}{k}: {yaml_str(v)}')
                        first = False
                else:
                    lines.append(f'  - {yaml_str(item)}')
        else:
            lines.append(f'{key}: {yaml_str(value)}')
    lines.append('---')
    return '\n'.join(lines) + '\n'


class Output:
    """Collects the generated files, then replaces the previous run's files with them (see the module docstring)."""

    def __init__(self):
        self.files = {}  # path relative to ROOT -> bytes, or Path of a file to copy

    def write(self, path, text):
        self.files[path.relative_to(ROOT).as_posix()] = text.encode('utf-8')

    def copy(self, src, dest):
        self.files[dest.relative_to(ROOT).as_posix()] = src

    def commit(self, force):
        old = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
        edited = [rel for rel, digest in old.items() if (ROOT / rel).exists() and sha256(ROOT / rel) != digest]
        # Files this script never wrote that it would now overwrite, e.g. a new post that took a migrated name
        foreign = [rel for rel in self.files if rel not in old and (ROOT / rel).exists()] if old else []
        if (edited or foreign) and not force:
            for rel in edited:
                print(f'changed since it was generated: {rel}', file=sys.stderr)
            for rel in foreign:
                print(f'not generated by this script: {rel}', file=sys.stderr)
            sys.exit('refusing to overwrite these files; re-run with --force to discard them')
        for rel in old:
            (ROOT / rel).unlink(missing_ok=True)
        manifest = {}
        for rel, data in sorted(self.files.items()):
            dest = ROOT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, Path):
                shutil.copy2(data, dest)
            else:
                dest.write_bytes(data)
            manifest[rel] = sha256(dest)
        MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    force = '--force' in sys.argv[1:]
    out = Output()
    posts = load('posts')
    meta = load('postmeta')
    users = {u['ID']: u for u in load('users')}
    terms = load('terms')
    comments = [c for c in load('comments') if c['comment_type'] != 'pingback']

    meta_by_post = defaultdict(lambda: defaultdict(list))
    for m in meta:
        meta_by_post[m['post_id']][m['meta_key']].append(m['meta_value'])
    terms_by_post = defaultdict(lambda: defaultdict(list))
    term_names = defaultdict(dict)
    for t in terms:
        terms_by_post[t['object_id']][t['taxonomy']].append(t['slug'])
        term_names[t['taxonomy']][t['slug']] = html.unescape(t['name'])

    content_rows = [p for p in posts if p['post_type'] in ('post', 'page')]
    rendered = wp_filters([('content', p['ID'], p['post_content']) for p in content_rows]
                          + [('title', p['ID'], p['post_title']) for p in content_rows]
                          + [('comment', c['comment_ID'], c['comment_content']) for c in comments])

    url_by_id = {}
    bodies = []
    redirects = []
    used_authors = set()
    used_categories = set()
    for p in content_rows:
        pid = p['ID']
        if p['post_type'] == 'page' and p['post_name'] in SKIPPED_PAGES:
            continue
        body = convert_body(rendered['content', pid])
        title = html.unescape(rendered['title', pid])
        if p['post_type'] == 'post':
            author = users[p['post_author']]
            used_authors.add(author['ID'])
            categories = terms_by_post[pid]['category']
            used_categories.update(categories)
            enclosure = enclosure_length = None
            for e in meta_by_post[pid]['enclosure']:
                # "<url>\n<length>\n<type>"
                url, length = (site_relative(line.strip(), prefix='^') for line in e.split('\n')[:2])
                enclosure, enclosure_length = url, int(length)
            post_comments = [{
                'author': html.unescape(c['comment_author']),
                'url': c['comment_author_url'] or None,
                'date': c['comment_date_gmt'] + ' +0000',
                'content': rendered['comment', c['comment_ID']].strip(),
            } for c in comments if c['comment_post_ID'] == pid]
            for c in post_comments:
                if c['url'] is None:
                    del c['url']
            fm = {
                'title': title,
                'date': wp_date(p['post_date'], p['post_date_gmt']),
                'author': author['user_nicename'],
                'description': html.unescape(re.sub(r'<[^>]+>', '', p['post_excerpt'])).strip() or None,
                'categories': categories,
                'tags': terms_by_post[pid]['post_tag'],
                'enclosure': enclosure,
                'enclosure-length': enclosure_length,
                # Episode 1 embeds its player in the text; the others only had the feed enclosure
                'show-enclosure': (enclosure is not None and enclosure not in body) or None,
                'comments': post_comments,
                'wp-id': pid,
                # Kept so feed readers don't see the migrated posts as new (see content/rss.xml)
                'wp-guid': p['guid'],
            }
            date = p['post_date_gmt'][:10]
            path = CONTENT / 'posts' / f'{date}-{p["post_name"]}.html'
            url_by_id[pid] = f'/posts/{p["post_name"]}/'
            for old in meta_by_post[pid]['_wp_old_slug']:
                redirects.append((f'/posts/{old}/', url_by_id[pid]))
        else:
            fm = {
                'title': title,
                'layout': 'page',
                'link': f'/{p["post_name"]}/',
                'wp-id': pid,
            }
            path = CONTENT / f'{p["post_name"]}.html'
            url_by_id[pid] = f'/{p["post_name"]}/'
        bodies.append(body)
        out.write(path, front_matter(fm) + '\n' + body.strip() + '\n')

    # Authors: data for the post layout, plus an archive page per author at the WordPress URL.
    authors_yml = ''
    for uid in sorted(used_authors):
        u = users[uid]
        authors_yml += (f'{u["user_nicename"]}:\n  name: {yaml_str(html.unescape(u["display_name"]))}\n'
                        f'  profile: {yaml_str("/author/" + u["user_nicename"] + "/")}\n  job: ""\n')
        out.write(CONTENT / 'author' / f'{u["user_nicename"]}.html', front_matter({
            'title': f'Author: {html.unescape(u["display_name"])}',
            'layout': 'author',
            'link': f'/author/{u["user_nicename"]}/',
            'author-id': u['user_nicename'],
        }))
    out.write(DATA / 'authors.yml', authors_yml)

    categories_yml = ''
    for slug in sorted(used_categories):
        categories_yml += f'{slug}:\n  name: {yaml_str(term_names["category"][slug])}\n'
        out.write(CONTENT / 'category' / f'{slug}.html', front_matter({
            'title': f'Category: {term_names["category"][slug]}',
            'layout': 'category',
            'link': f'/category/{slug}/',
            'category': slug,
        }))
    out.write(DATA / 'categories.yml', categories_yml)

    # Menu, rebuilt from the WordPress nav menu: links, and custom items without URL ("Channel Rules",
    # "Channel Data") that only hold a submenu.
    items = sorted((p for p in posts if p['post_type'] == 'nav_menu_item'), key=lambda p: p['menu_order'])
    titles = {p['ID']: p for p in posts}
    menu = []
    submenus = {}
    for item in items:
        m = {k: v[0] for k, v in meta_by_post[item['ID']].items()}
        parent = int(m['_menu_item_menu_item_parent'])
        if m['_menu_item_type'] == 'custom' and not m['_menu_item_url']:
            entry = {'title': item['post_title'], 'children': []}
            submenus[item['ID']] = entry['children']
        elif m['_menu_item_type'] == 'custom':
            entry = {'title': item['post_title'], 'path': m['_menu_item_url']}
        else:
            target = titles[int(m['_menu_item_object_id'])]
            entry = {'title': item['post_title'] or html.unescape(target['post_title']),
                     'path': url_by_id[target['ID']]}
        (submenus[parent] if parent else menu).append(entry)
    menu_yml = '# Generated from the WordPress navigation menu by migration/convert.py\nitems:\n'
    for e in menu:
        menu_yml += f'  - title: {yaml_str(e["title"])}\n'
        if 'children' in e:
            menu_yml += '    children:\n'
            for c in e['children']:
                menu_yml += f'      - title: {yaml_str(c["title"])}\n        path: {yaml_str(c["path"])}\n'
        else:
            menu_yml += f'    path: {yaml_str(e["path"])}\n'
    out.write(DATA / 'menu.yml', menu_yml)

    # WordPress post/page IDs, for ?p=<id> / ?page_id=<id> links (resolved client-side, see templates).
    out.write(DATA / 'wp-ids.yml', '# WordPress post IDs to URLs, generated by migration/convert.py\nids:\n'
              + ''.join(f'  "{pid}": {yaml_str(url)}\n' for pid, url in sorted(url_by_id.items())))

    # Uploads: small files at their original path, big ones in media/ with a redirect to GitHub. WordPress generated
    # resized copies (name-150x150.png) of every image; only those that the content embeds are kept.
    all_bodies = '\n'.join(bodies)
    for src in sorted((MIGRATION / 'uploads').rglob('*')):
        if not src.is_file():
            continue
        rel = Path('wp-content') / src.relative_to(MIGRATION)
        if re.search(r'-\d+x\d+\.\w+$', src.name) and f'/{rel.as_posix()}' not in all_bodies:
            continue
        if src.stat().st_size > MAX_PAGES_FILE:
            dest = MEDIA / rel
            redirects.append((f'/{rel}', f'{RAW_BASE}/{rel}'))
        else:
            dest = PUBLIC / rel
        out.copy(src, dest)

    redirects_txt = '# Generated by migration/convert.py\n'
    seen = set()
    for src, dest in redirects:
        # An old slug reused by several posts: WordPress redirected to one of them, keep the first.
        if src in seen:
            continue
        seen.add(src)
        redirects_txt += f'{src} {dest} 301\n'
    out.write(PAGES_DIR / '_redirects.generated', redirects_txt)

    out.commit(force)

    print(f'{sum(1 for p in content_rows if p["post_type"] == "post")} posts, '
          f'{sum(1 for p in content_rows if p["post_type"] == "page") - len(SKIPPED_PAGES)} pages, '
          f'{len(comments)} comments, {len(redirects)} redirects')


if __name__ == '__main__':
    main()
