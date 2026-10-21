#!/usr/bin/env python3
"""Checks the generated site (target/roq, including _redirects) against the WordPress export: every published post and
page must exist at its WordPress URL with its title, author and date, internal links must resolve, and no file may
exceed the Cloudflare Pages size limit. Exits non-zero on failure."""
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ROOT / 'migration' / 'wp-export'
OUT = ROOT / 'target' / 'roq'
MAX_FILE = 25 * 1024 * 1024
SKIPPED_PAGES = {'sample-page'}

errors = []


def load(name):
    with open(EXPORT / f'{name}.jsonl', encoding='utf-8') as f:
        return [json.loads(line) for line in f]


def read_redirects():
    rules = []
    for line in (OUT / '_redirects').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        src, dest, *_ = line.split()
        pattern = re.escape(src).replace(r'\*', '.*')
        pattern = re.sub(r':\w+', '[^/]+', pattern)
        rules.append((src, re.compile(pattern + '$'), dest))
    return rules


def exists(path):
    path = unquote(path).lstrip('/')
    target = OUT / path
    return target.is_file() or (target / 'index.html').is_file()


# wptexturize's typographic replacements, which the migrated titles carry
TYPOGRAPHY = str.maketrans({'\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"', '\u2013': '-', '\u2014': '-',
                            '\u2032': "'", '\u2033': '"', '\u00d7': 'x', '\u00a0': ' '})


def norm(text):
    text = html.unescape(text).replace('\u2026', '...').translate(TYPOGRAPHY)
    return re.sub(r'\s+', ' ', text)


def main():
    posts = load('posts')
    users = {u['ID']: u for u in load('users')}
    redirects = read_redirects()

    def resolves(path):
        return exists(path) or any(rx.match(path) for _, rx, _ in redirects)

    n_posts = n_pages = 0
    for p in posts:
        if p['post_type'] == 'post':
            n_posts += 1
            url = f'/posts/{p["post_name"]}/'
        elif p['post_type'] == 'page' and p['post_name'] not in SKIPPED_PAGES:
            n_pages += 1
            url = f'/{p["post_name"]}/'
        else:
            continue
        file = OUT / url.strip('/') / 'index.html'
        if not file.is_file():
            errors.append(f'missing {url} ({p["post_title"]})')
            continue
        page = norm(file.read_text(encoding='utf-8'))
        if norm(p['post_title']) not in page:
            errors.append(f'{url}: title {p["post_title"]!r} not found')
        if p['post_type'] == 'post':
            author = norm(users[p['post_author']]['display_name'])
            if author not in page:
                errors.append(f'{url}: author {author!r} not found')
            date = p['post_date'][:10]
            if f'datetime="{date}' not in page and date not in page:
                errors.append(f'{url}: date {date} not found')

    expected = sum(1 for p in posts if p['post_type'] == 'post'), \
        sum(1 for p in posts if p['post_type'] == 'page') - len(SKIPPED_PAGES)
    if (n_posts, n_pages) != expected:
        errors.append(f'expected {expected} posts/pages, checked {(n_posts, n_pages)}')
    generated_posts = [d for d in (OUT / 'posts').iterdir() if d.is_dir() and d.name != 'tag']
    if len(generated_posts) != expected[0]:
        errors.append(f'{len(generated_posts)} post directories generated, expected {expected[0]}')

    # Redirects must not shadow generated content (Cloudflare Pages applies them before serving files)
    for src, _, _ in redirects:
        if ':' not in src and '*' not in src and exists(src) and src.rstrip('/') not in ('', '/posts'):
            errors.append(f'redirect source {src} shadows a generated page')

    # Internal links and assets
    broken = {}
    for file in OUT.rglob('*.html'):
        text = file.read_text(encoding='utf-8')
        for ref in re.findall(r'(?:href|src)="(/[^"]*)"', text):
            path = urlsplit(html.unescape(ref)).path
            if path.startswith('//') or not path:
                continue
            if not resolves(path):
                broken.setdefault(path, file.relative_to(OUT))
    for path, file in sorted(broken.items()):
        errors.append(f'broken link {path} (in {file})')

    for file in OUT.rglob('*'):
        if file.is_file() and file.stat().st_size > MAX_FILE:
            errors.append(f'{file.relative_to(OUT)} exceeds 25 MiB')

    for e in errors:
        print(e)
    print(f'checked {n_posts} posts, {n_pages} pages: {len(errors)} problem(s)')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
