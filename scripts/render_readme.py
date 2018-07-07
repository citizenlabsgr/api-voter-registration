"""Render the static homepage from the README."""

import sys
from pathlib import Path

import grip


def run(input_markdown_path, output_html_path):
    markdown = ""

    with Path(input_markdown_path).open('r') as f:
        for line in f:

            # Remove developer badges
            if 'circleci.com' in line:
                continue
            if 'waffle.io' in line:
                continue

            # Convert to relative links for review and staging
            line = line.replace('(https://michiganelections.io', '(')

            markdown += line

    html = grip.render_page(
        text=markdown, title="README.md", render_inline=True
    )

    with Path(output_html_path).open('w') as f:
        f.write(html)


def main():
    run(sys.argv[1], sys.argv[2])