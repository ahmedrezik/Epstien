#!/usr/bin/env python3
"""
Search Epstein files for mentions of your contacts (LinkedIn and/or X/Twitter).

Usage:
    python EpsteIn.py --connections <linkedin_csv> [--output <report.html>]
    python EpsteIn.py --x-following <following.js> --x-bearer-token <token>

Prerequisites:
    pip install requests
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

API_BASE_URL = "https://analytics.dugganusa.com/api/v1/search"
PDF_BASE_URL = "https://www.justice.gov/epstein/files/"


def parse_linkedin_contacts(csv_path):
    """
    Parse LinkedIn connections CSV export.
    LinkedIn exports have columns: First Name, Last Name, Email Address, Company, Position, Connected On
    """
    contacts = []

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        # Skip lines until we find the header row
        # LinkedIn includes a "Notes" section at the top that must be skipped.
        header_line = None
        for line in f:
            if 'First Name' in line and 'Last Name' in line:
                header_line = line
                break

        if not header_line:
            return contacts

        # Create a reader from the header line onwards
        remaining_content = header_line + f.read()
        reader = csv.DictReader(remaining_content.splitlines())

        for row in reader:
            first_name = row.get('First Name', '').strip()
            last_name = row.get('Last Name', '').strip()

            # Remove credentials/certifications (everything after the first comma)
            if ',' in last_name:
                last_name = last_name.split(',')[0].strip()

            if first_name and last_name:
                full_name = f"{first_name} {last_name}"
                contacts.append({
                    'first_name': first_name,
                    'last_name': last_name,
                    'full_name': full_name,
                    'company': row.get('Company', ''),
                    'position': row.get('Position', '')
                })

    return contacts


def parse_x_following(js_path):
    """
    Parse X/Twitter data export following.js file.
    Returns a list of account ID strings.
    """
    with open(js_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Strip the JS variable assignment prefix
    prefix = 'window.YTD.following.part0 = '
    if not content.startswith(prefix):
        print(f"Error: {js_path} doesn't look like an X/Twitter following.js export.", file=sys.stderr)
        print(f"Expected file to start with: {prefix}", file=sys.stderr)
        sys.exit(1)

    json_str = content[len(prefix):]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse JSON in {js_path}: {e}", file=sys.stderr)
        sys.exit(1)

    account_ids = []
    for entry in data:
        following = entry.get('following', {})
        account_id = following.get('accountId', '')
        if account_id:
            account_ids.append(account_id)

    return account_ids


def resolve_x_ids_to_names(account_ids, bearer_token):
    """
    Resolve X/Twitter account IDs to display names via the X API v2.
    Returns contact dicts matching the shape used by parse_linkedin_contacts.
    """
    contacts = []
    # API allows max 100 IDs per request
    batch_size = 100

    for i in range(0, len(account_ids), batch_size):
        batch = account_ids[i:i + batch_size]
        ids_param = ','.join(batch)
        url = f"https://api.x.com/2/users?ids={ids_param}"
        headers = {'Authorization': f'Bearer {bearer_token}'}

        delay = 1
        while True:
            try:
                response = requests.get(url, headers=headers, timeout=30)

                if response.status_code == 401:
                    print("Error: X API authentication failed. Check your bearer token.", file=sys.stderr)
                    sys.exit(1)
                if response.status_code == 403:
                    print("Error: X API access forbidden. Your bearer token may lack the required permissions.", file=sys.stderr)
                    sys.exit(1)
                if response.status_code == 429:
                    retry_after = response.headers.get('Retry-After')
                    wait = int(retry_after) if retry_after else delay
                    print(f"  [X API rate limited, retrying in {wait}s]", flush=True)
                    time.sleep(wait)
                    delay *= 2
                    continue

                response.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                print(f"Error: X API request failed: {e}", file=sys.stderr)
                sys.exit(1)

        data = response.json()

        # Warn about suspended/deleted accounts
        for error in data.get('errors', []):
            print(f"  Warning: {error.get('detail', 'Unknown error for account')}", file=sys.stderr)

        for user in data.get('data', []):
            name = user.get('name', '').strip()
            if not name:
                continue

            parts = name.split(None, 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''
            handle = user.get('username', '')

            contacts.append({
                'first_name': first_name,
                'last_name': last_name,
                'full_name': name,
                'company': '',
                'position': f'@{handle}' if handle else ''
            })

    return contacts


def search_epstein_files(name, delay):
    """
    Search the Epstein files API for a name.
    Returns (result_dict, delay) where delay may be increased on 429 responses.
    """
    # Wrap name in quotes for exact phrase matching
    quoted_name = f'"{name}"'
    encoded_name = urllib.parse.quote(quoted_name)
    url = f"{API_BASE_URL}?q={encoded_name}&indexes=epstein_files"

    while True:
        try:
            response = requests.get(url, timeout=30)

            if response.status_code == 429:
                retry_after = response.headers.get('Retry-After')

                if retry_after:
                    delay = int(retry_after)
                else:
                    delay *= 2

                print(f" [429 rate limited, retrying in {delay}s]", end='', flush=True)
                time.sleep(delay)
                continue

            response.raise_for_status()
            data = response.json()

            if data.get('success'):
                return {
                    'total_hits': data.get('data', {}).get('totalHits', 0),
                    'hits': data.get('data', {}).get('hits', []),
                    'error': None
                }, delay
        except requests.exceptions.RequestException as e:
            return {'total_hits': 0, 'hits': [], 'error': str(e)}, delay

        return {'total_hits': 0, 'hits': [], 'error': None}, delay


def highlight_name_in_preview(preview_text, first_name, last_name):
    """
    Highlight contact name parts in a preview string using <mark> tags.
    Matches full name first, then last name, then first name (2+ chars).
    """
    full_name = f"{first_name} {last_name}"
    highlighted = html.escape(preview_text)

    # Full name match
    pattern = re.compile(re.escape(html.escape(full_name)), re.IGNORECASE)
    highlighted = pattern.sub(
        lambda m: f'<mark>{m.group(0)}</mark>', highlighted
    )

    # Last name match (word boundary, skip if already inside <mark>)
    if last_name and len(last_name) >= 2:
        pattern = re.compile(
            r'(?<!<mark>)\b(' + re.escape(html.escape(last_name)) + r')\b(?!</mark>)',
            re.IGNORECASE
        )
        highlighted = pattern.sub(r'<mark>\1</mark>', highlighted)

    # First name match (word boundary, 2+ chars, skip if already inside <mark>)
    if first_name and len(first_name) >= 2:
        pattern = re.compile(
            r'(?<!<mark>)\b(' + re.escape(html.escape(first_name)) + r')\b(?!</mark>)',
            re.IGNORECASE
        )
        highlighted = pattern.sub(r'<mark>\1</mark>', highlighted)

    return highlighted


def print_progress_bar(current, total, name, hits, is_tty):
    """
    Print a progress bar for CLI output.
    Falls back to line-by-line when not a TTY.
    """
    if not is_tty:
        print(f"  [{current}/{total}] {name} -> {hits} hits", flush=True)
        return

    percent = current / total
    bar_width = 30
    filled = int(bar_width * percent)
    bar = '#' * filled + '-' * (bar_width - filled)

    # Truncate name to fit in terminal
    max_name_len = 30
    display_name = name if len(name) <= max_name_len else name[:max_name_len - 3] + '...'

    line = f"\r  [{bar}] {percent:>4.0%} ({current}/{total}) {display_name} -> {hits} hits"
    # Pad with spaces to clear previous longer lines
    print(f"{line:<80}", end='', flush=True)


def generate_html_report(results, output_path, total_contacts=None, was_interrupted=False):
    contacts_with_mentions = len([r for r in results if r['total_mentions'] > 0])
    total_searched = len(results)
    if total_contacts is None:
        total_contacts = total_searched
    total_hits = sum(r['total_mentions'] for r in results)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    logo_html = """<div class="logo" role="img" aria-label="EpsteIn">
        <span class="logo-icon">E</span>
    </div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EpsteIn: Which of Your Contacts Appear in the Epstein Files?</title>
    <style>
        :root {{
            --bg-primary: #f5f5f5;
            --bg-card: #fff;
            --bg-hit: #f9f9f9;
            --text-primary: #333;
            --text-secondary: #555;
            --text-preview: #444;
            --text-muted: #6b6b6b;
            --border-light: #eee;
            --border-medium: #ddd;
            --accent-blue: #3498db;
            --accent-red: #e74c3c;
            --shadow-color: rgba(0,0,0,0.1);
            --logo-bg: #000;
            --logo-text: #fff;
            --highlight-bg: #fff3cd;
            --highlight-dark-bg: #b8860b;
            --search-bg: #fff;
            --search-border: #ddd;
            --toggle-bg: transparent;
            --toggle-border: #3498db;
            --toggle-text: #3498db;
        }}

        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg-primary: #1a1a2e;
                --bg-card: #16213e;
                --bg-hit: #0f3460;
                --text-primary: #e0e0e0;
                --text-secondary: #b0b0b0;
                --text-preview: #c0c0c0;
                --text-muted: #999;
                --border-light: #2a2a4a;
                --border-medium: #3a3a5a;
                --accent-blue: #5dade2;
                --accent-red: #e74c3c;
                --shadow-color: rgba(0,0,0,0.3);
                --logo-bg: #fff;
                --logo-text: #000;
                --highlight-bg: #b8860b;
                --highlight-dark-bg: #fff3cd;
                --search-bg: #16213e;
                --search-border: #3a3a5a;
                --toggle-bg: transparent;
                --toggle-border: #5dade2;
                --toggle-text: #5dade2;
            }}
        }}

        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: var(--bg-primary);
            color: var(--text-primary);
        }}
        .logo {{
            display: flex;
            justify-content: center;
            margin: 0 auto 20px auto;
        }}
        .logo-icon {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 80px;
            height: 80px;
            background: var(--logo-bg);
            border-radius: 16px;
            color: var(--logo-text);
            font-size: 3.2rem;
            font-weight: 800;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1;
        }}
        .summary {{
            background: var(--bg-card);
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 10px;
            box-shadow: 0 2px 4px var(--shadow-color);
        }}
        .meta {{
            font-size: 0.85em;
            color: var(--text-muted);
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid var(--border-light);
        }}
        .search-bar {{
            position: sticky;
            top: 0;
            z-index: 100;
            background: var(--bg-primary);
            padding: 10px 0 10px 0;
            margin-bottom: 20px;
        }}
        .search-bar input {{
            width: 100%;
            padding: 12px 16px;
            font-size: 16px;
            border: 2px solid var(--search-border);
            border-radius: 8px;
            background: var(--search-bg);
            color: var(--text-primary);
            outline: none;
        }}
        .search-bar input:focus {{
            border-color: var(--accent-blue);
        }}
        .search-bar input::placeholder {{
            color: var(--text-muted);
        }}
        .search-count {{
            font-size: 0.85em;
            color: var(--text-muted);
            margin-top: 6px;
        }}
        .contact {{
            background: var(--bg-card);
            padding: 20px;
            margin-bottom: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px var(--shadow-color);
        }}
        .contact-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-light);
            padding-bottom: 10px;
            margin-bottom: 15px;
        }}
        .contact-name {{
            font-size: 1.4em;
            font-weight: bold;
            color: var(--text-primary);
        }}
        .contact-info {{
            color: var(--text-secondary);
            font-size: 0.9em;
        }}
        .hit-count {{
            background: var(--accent-red);
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-weight: bold;
            white-space: nowrap;
        }}
        .hit {{
            background: var(--bg-hit);
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
            border-left: 3px solid var(--accent-blue);
        }}
        .hit-preview {{
            color: var(--text-preview);
            margin-bottom: 10px;
            font-size: 0.95em;
        }}
        .hit-preview mark {{
            background: var(--highlight-bg);
            color: inherit;
            padding: 1px 2px;
            border-radius: 2px;
        }}
        .hit-link {{
            display: inline-block;
            color: var(--accent-blue);
            text-decoration: none;
            font-size: 0.85em;
        }}
        .hit-link:hover {{
            text-decoration: underline;
        }}
        .no-results {{
            color: var(--text-muted);
            font-style: italic;
        }}
        .hits-toggle {{
            display: inline-block;
            margin-top: 8px;
            padding: 6px 14px;
            font-size: 0.85em;
            color: var(--toggle-text);
            background: var(--toggle-bg);
            border: 1px solid var(--toggle-border);
            border-radius: 4px;
            cursor: pointer;
        }}
        .hits-toggle:hover {{
            opacity: 0.8;
        }}
        .hits-hidden {{
            display: none;
        }}

        @media (max-width: 600px) {{
            body {{
                padding: 10px;
            }}
            .contact-header {{
                flex-direction: column;
                align-items: flex-start;
                gap: 8px;
            }}
            .contact-name {{
                font-size: 1.1em;
            }}
            .hit {{
                padding: 10px;
            }}
            .hit-preview {{
                font-size: 0.85em;
            }}
        }}
    </style>
</head>
<body>
<header>
    {logo_html}
</header>

<main>
    <div class="summary">
        <strong>Total contacts searched:</strong> {total_searched}<br>
        <strong>Contacts with mentions:</strong> {contacts_with_mentions}<br>
        <strong>Total hits:</strong> {total_hits:,}
        {'<br><em>Note: Search was interrupted. This is a partial report.</em>' if was_interrupted else ''}
        <div class="meta">
            Generated on {timestamp} &middot; {total_searched} of {total_contacts} contacts searched
        </div>
    </div>

    <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Filter contacts by name, company, or position..." aria-label="Filter contacts">
        <div class="search-count" id="searchCount">Showing {contacts_with_mentions} of {contacts_with_mentions} contacts</div>
    </div>
"""

    for result in results:
        if result['total_mentions'] == 0:
            continue

        contact_info = []
        if result['position']:
            contact_info.append(html.escape(result['position']))
        if result['company']:
            contact_info.append(html.escape(result['company']))

        search_data = f"{result['name']} {result['position']} {result['company']}".lower()

        html_content += f"""
    <div class="contact" data-search="{html.escape(search_data)}">
        <div class="contact-header">
            <div>
                <div class="contact-name">{html.escape(result['name'])}</div>
                <div class="contact-info">{' at '.join(contact_info) if contact_info else ''}</div>
            </div>
            <div class="hit-count">{result['total_mentions']:,} mentions</div>
        </div>
"""

        if result['hits']:
            visible_count = 3
            for idx, hit in enumerate(result['hits']):
                preview = hit.get('content_preview') or (hit.get('content') or '')[:500]
                file_path = hit.get('file_path', '')
                if file_path:
                    file_path = file_path.replace('dataset', 'DataSet')
                    base_url = PDF_BASE_URL.rstrip('/') if file_path.startswith('/') else PDF_BASE_URL
                    pdf_url = base_url + urllib.parse.quote(file_path, safe='/')
                else:
                    pdf_url = ''

                highlighted_preview = highlight_name_in_preview(
                    preview, result['first_name'], result['last_name']
                )

                hidden_class = ' hits-hidden' if idx >= visible_count else ''

                html_content += f"""
        <div class="hit{hidden_class}"{' data-extra="true"' if idx >= visible_count else ''}>
            <div class="hit-preview">{highlighted_preview}</div>
            {f'<a class="hit-link" href="{html.escape(pdf_url)}" target="_blank" rel="noopener noreferrer">View PDF: {html.escape(file_path)}</a>' if pdf_url else ''}
        </div>
"""

            extra_count = len(result['hits']) - visible_count
            if extra_count > 0:
                html_content += f"""
        <button class="hits-toggle" onclick="toggleHits(this)" aria-expanded="false">Show {extra_count} more hit{'s' if extra_count != 1 else ''}</button>
"""
        else:
            html_content += """
        <div class="no-results">Hit details not available</div>
"""

        html_content += """
    </div>
"""

    html_content += f"""
</main>

<footer>
    <div style="margin-top:40px;padding-top:20px;border-top:1px solid var(--border-medium);text-align:center;color:var(--text-secondary);font-size:0.9em;">
        Epstein files indexed by <a href="https://dugganusa.com" target="_blank" rel="noopener noreferrer" style="color:var(--accent-blue);text-decoration:none;">DugganUSA.com</a>
    </div>
</footer>

<script>
function toggleHits(btn) {{
    var card = btn.closest('.contact');
    var extras = card.querySelectorAll('[data-extra="true"]');
    var expanded = btn.getAttribute('aria-expanded') === 'true';
    for (var i = 0; i < extras.length; i++) {{
        extras[i].classList.toggle('hits-hidden');
    }}
    expanded = !expanded;
    btn.setAttribute('aria-expanded', String(expanded));
    if (expanded) {{
        btn.textContent = 'Show fewer hits';
    }} else {{
        var count = extras.length;
        btn.textContent = 'Show ' + count + ' more hit' + (count !== 1 ? 's' : '');
    }}
}}

(function() {{
    var input = document.getElementById('searchInput');
    var countEl = document.getElementById('searchCount');
    var cards = document.querySelectorAll('.contact');
    var totalCards = cards.length;

    input.addEventListener('input', function() {{
        var query = this.value.toLowerCase();
        var shown = 0;
        for (var i = 0; i < cards.length; i++) {{
            var searchData = cards[i].getAttribute('data-search') || '';
            if (!query || searchData.indexOf(query) !== -1) {{
                cards[i].style.display = '';
                shown++;
            }} else {{
                cards[i].style.display = 'none';
            }}
        }}
        countEl.textContent = 'Showing ' + shown + ' of ' + totalCards + ' contacts';
    }});
}})();
</script>
</body>
</html>
"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)


def main():
    if not HAS_REQUESTS:
        print("Error: 'requests' library is required. Install with: pip install requests", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Search Epstein files for mentions of your contacts'
    )
    parser.add_argument(
        '--connections', '-c',
        required=False,
        help='Path to LinkedIn connections CSV export'
    )
    parser.add_argument(
        '--x-following',
        required=False,
        help='Path to X/Twitter data export following.js file'
    )
    parser.add_argument(
        '--x-bearer-token',
        required=False,
        default=os.environ.get('X_BEARER_TOKEN'),
        help='X API bearer token (or set X_BEARER_TOKEN env var)'
    )
    parser.add_argument(
        '--output', '-o',
        default='EpsteIn.html',
        help='Output HTML file for the report (default: EpsteIn.html)'
    )
    args = parser.parse_args()

    # Validate inputs — require at least one source
    if not args.connections and not args.x_following:
        print("""
No contact source specified. Provide at least one:

  LinkedIn:
     python EpsteIn.py --connections /path/to/Connections.csv

  X/Twitter:
     python EpsteIn.py --x-following /path/to/following.js --x-bearer-token YOUR_TOKEN

  Both:
     python EpsteIn.py --connections Connections.csv --x-following following.js --x-bearer-token YOUR_TOKEN

To export your LinkedIn connections:
  1. Go to linkedin.com and log in
  2. Click your profile icon > Settings & Privacy > Data privacy
  3. Click "Get a copy of your data" and select Connections
  4. Download and extract the ZIP file

To export your X/Twitter following list:
  1. Go to x.com > Settings > Your Account > Download an archive of your data
  2. Wait for X's email, then download and extract the archive
  3. Locate data/following.js in the extracted archive
""")
        sys.exit(1)

    if args.x_following and not args.x_bearer_token:
        print("Error: --x-bearer-token (or X_BEARER_TOKEN env var) is required when using --x-following.", file=sys.stderr)
        sys.exit(1)

    contacts = []

    # Parse LinkedIn connections
    if args.connections:
        if not os.path.exists(args.connections):
            print(f"Error: Connections file not found: {args.connections}", file=sys.stderr)
            sys.exit(1)
        print(f"Reading LinkedIn connections from: {args.connections}")
        linkedin_contacts = parse_linkedin_contacts(args.connections)
        print(f"Found {len(linkedin_contacts)} LinkedIn connections")
        contacts.extend(linkedin_contacts)

    # Parse X/Twitter following
    if args.x_following:
        if not os.path.exists(args.x_following):
            print(f"Error: Following file not found: {args.x_following}", file=sys.stderr)
            sys.exit(1)
        print(f"Reading X/Twitter following from: {args.x_following}")
        account_ids = parse_x_following(args.x_following)
        print(f"Found {len(account_ids)} followed accounts, resolving names via X API...")
        x_contacts = resolve_x_ids_to_names(account_ids, args.x_bearer_token)
        print(f"Resolved {len(x_contacts)} X/Twitter accounts")
        contacts.extend(x_contacts)

    # Deduplicate by name (case-insensitive), keeping first occurrence
    seen_names = set()
    unique_contacts = []
    for contact in contacts:
        key = contact['full_name'].lower()
        if key not in seen_names:
            seen_names.add(key)
            unique_contacts.append(contact)
    contacts = unique_contacts

    if not contacts:
        print("No contacts found. Check your input files.", file=sys.stderr)
        sys.exit(1)

    print(f"Total unique contacts to search: {len(contacts)}")

    # Search for each contact
    print("Searching Epstein files API...")
    print("(Press Ctrl+C to stop and generate a partial report)\n")
    results = []
    failed_searches = []
    was_interrupted = False
    is_tty = sys.stdout.isatty()

    delay = 0.25

    try:
        for i, contact in enumerate(contacts):
            search_result, delay = search_epstein_files(contact['full_name'], delay)
            total_mentions = search_result['total_hits']

            print_progress_bar(i + 1, len(contacts), contact['full_name'], total_mentions, is_tty)

            if search_result.get('error'):
                failed_searches.append((contact['full_name'], search_result['error']))

            results.append({
                'name': contact['full_name'],
                'first_name': contact['first_name'],
                'last_name': contact['last_name'],
                'company': contact['company'],
                'position': contact['position'],
                'total_mentions': total_mentions,
                'hits': search_result['hits']
            })

            # Rate limiting
            if i < len(contacts) - 1:
                time.sleep(delay)

    except KeyboardInterrupt:
        was_interrupted = True
        if is_tty:
            print()  # Clear progress bar line
        print("\nSearch interrupted by user (Ctrl+C).")
        if not results:
            print("No results collected yet. Exiting without generating report.")
            sys.exit(0)
        print(f"Generating partial report with {len(results)} of {len(contacts)} contacts searched...")

    if is_tty:
        print()  # Newline after progress bar

    # Sort by mentions (descending)
    results.sort(key=lambda x: x['total_mentions'], reverse=True)

    # Write HTML report
    print(f"Writing report to: {args.output}")
    generate_html_report(results, args.output, total_contacts=len(contacts), was_interrupted=was_interrupted)

    # Print summary
    contacts_with_mentions = [r for r in results if r['total_mentions'] > 0]
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Total contacts searched: {len(results)}")
    print(f"Contacts with mentions: {len(contacts_with_mentions)}")

    if contacts_with_mentions:
        print(f"\nTop mentions:")
        for r in contacts_with_mentions[:20]:
            print(f"  {r['total_mentions']:6,} - {r['name']}")
    else:
        print("\nNo contacts found in the Epstein files.")

    if failed_searches:
        print(f"\nFailed searches ({len(failed_searches)}):")
        for name, error in failed_searches[:10]:
            print(f"  {name}: {error}")
        if len(failed_searches) > 10:
            print(f"  ... and {len(failed_searches) - 10} more")

    print(f"\nFull report saved to: {args.output}")


if __name__ == '__main__':
    main()
