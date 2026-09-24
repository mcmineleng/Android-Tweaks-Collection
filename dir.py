#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描目录，按目录树缩进输出。

格式（缩进用两个空格）:
  FolderName:
    SubFolder:
      [FileName:][FileSize:][Modified:]Url

规则（每个本地文件 = 一个 entry）:
- FileName = 本地文件名
- 取文件内容的第一行非空非注释行作为 line:
    - line 是 http(s):// 开头  → URL = line,            元数据从远程获取
    - line 是 / 开头           → URL = line,            元数据从本地 stat
    - 其他（普通内容）         → URL = REPO_RAW + 本地文件名,
                                元数据从本地 stat
- Modified 输出为 Unix 时间戳（秒）

--exclude 可多次指定，路径相对于扫描目录，支持 glob 通配符（fnmatch 风格）。
匹配是基于扫描目录的完整相对路径；若匹配到目录，则整棵子树跳过。
"""

import os
import sys
import argparse
import fnmatch
from email.utils import parsedate_to_datetime
import urllib.request
import urllib.error


INDENT = "  "


def log(msg):
    print(msg, file=sys.stderr)


def parse_http_date(value):
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return int(dt.timestamp())
    except Exception:
        log(f"[WARN] 无法解析 Last-Modified: {value}")
        return None


def get_remote_info(url):
    """返回 (size, modified_ts)，失败项为 None"""
    size = None
    modified = None
    try:
        req = urllib.request.Request(url, method='HEAD')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                cl = resp.headers.get('Content-Length')
                if cl is not None:
                    size = int(cl)
                lm = resp.headers.get('Last-Modified')
                if lm:
                    modified = parse_http_date(lm)
        except urllib.error.HTTPError as e:
            if e.code in (405, 501):
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=15) as resp:
                    cl = resp.headers.get('Content-Length')
                    if cl is not None:
                        size = int(cl)
                    lm = resp.headers.get('Last-Modified')
                    if lm:
                        modified = parse_http_date(lm)
            else:
                log(f"[WARN] 获取远程信息失败 (HTTP {e.code}): {url}")
    except Exception as e:
        log(f"[WARN] 获取远程信息失败: {url} - {e}")
    return size, modified


def get_local_info(path):
    size = None
    modified = None
    try:
        st = os.stat(path)
        size = st.st_size
        modified = int(st.st_mtime)
    except Exception as e:
        log(f"[WARN] 获取本地文件信息失败: {path} - {e}")
    return size, modified


def is_excluded(rel_path, exclude_patterns):
    rel_path = rel_path.replace(os.sep, '/').strip('/')
    parts = rel_path.split('/')
    prefixes = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]

    for pattern in exclude_patterns:
        pattern = pattern.replace('\\', '/').strip('/')
        for prefix in prefixes:
            if fnmatch.fnmatch(prefix, pattern):
                return True
    return False


def process_file(filepath):
    """返回 (filename, size, modified, url) 或 None。"""
    filename = os.path.basename(filepath)

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        log(f"[ERROR] 无法读取文件: {filepath} - {e}")
        return None

    content_line = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        content_line = line
        break

    if content_line is None:
        log(f"[WARN] 文件为空或无有效内容: {filepath}")
        return None

    if content_line.startswith('http://') or content_line.startswith('https://'):
        url = content_line
        log(f"[INFO] 远程获取: {url}")
        size, modified = get_remote_info(url)

    elif content_line.startswith('/'):
        url = content_line
        log(f"[INFO] 本地获取: {url}")
        size, modified = get_local_info(url)

    else:
        repo_raw = os.environ.get('REPO_RAW', '').rstrip('/')
        if not repo_raw:
            log(f"[WARN] 普通内容但未设置 REPO_RAW，URL 置空: {filepath}")
            url = ''
        else:
            url = f"{repo_raw}/{filename}"
        log(f"[INFO] 本地元数据 + REPO_RAW 拼接: {url}")
        size, modified = get_local_info(filepath)

    return (filename, size, modified, url)


def format_entry(filename, size, modified, url):
    parts = [filename + ":"]
    if size is not None:
        parts.append(str(size) + ":")
    if modified is not None:
        parts.append(str(modified) + ":")
    parts.append(url)
    return "".join(parts)


def build_tree(scan_dir, exclude_patterns):
    """
    返回嵌套结构:
      {'dirs': {name: node, ...}, 'files': [(filename, filepath), ...]}
    """
    root_node = {'dirs': {}, 'files': []}
    skipped_count = 0
    file_count = 0

    for cur_root, dirs, filenames in os.walk(scan_dir):
        rel_dir = os.path.relpath(cur_root, scan_dir)
        if rel_dir == '.':
            rel_dir = ''

        kept_dirs = []
        for d in sorted(dirs):
            full = os.path.join(cur_root, d)
            rel = os.path.relpath(full, scan_dir)
            if is_excluded(rel, exclude_patterns):
                log(f"[INFO] 排除目录: {rel}")
                skipped_count += 1
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        node = root_node
        if rel_dir:
            for part in rel_dir.split(os.sep):
                node = node['dirs'].setdefault(part, {'dirs': {}, 'files': []})

        for fn in sorted(filenames):
            filepath = os.path.join(cur_root, fn)
            rel_path = os.path.relpath(filepath, scan_dir)
            if is_excluded(rel_path, exclude_patterns):
                log(f"[INFO] 排除文件: {rel_path}")
                skipped_count += 1
                continue
            node['files'].append((fn, filepath))
            file_count += 1

    return root_node, file_count, skipped_count


def render_node(node, name, indent_level, out_lines):
    """
    渲染一个目录节点: 先 `名字:`，再文件 entries，再子目录。
    """
    prefix = INDENT * indent_level
    out_lines.append(f"{prefix}{name}:")

    child_prefix = INDENT * (indent_level + 1)

    for fn, filepath in node['files']:
        log(f"[INFO] 处理文件: {filepath}")
        entry = process_file(filepath)
        if entry:
            filename, size, modified, url = entry
            out_lines.append(child_prefix + format_entry(filename, size, modified, url))

    for sub_name in sorted(node['dirs'].keys()):
        render_node(node['dirs'][sub_name], sub_name, indent_level + 1, out_lines)


def main():
    parser = argparse.ArgumentParser(
        description='扫描目录，按目录树缩进输出（不含扫描根目录名）。'
    )
    parser.add_argument('scan_dir', help='要扫描的目录')
    parser.add_argument('output_file', nargs='?', default=None,
                        help='输出文件路径（可选，不指定则输出到 stdout）')
    parser.add_argument('--exclude', action='append', default=[],
                        metavar='PATTERN',
                        help='排除的文件或目录（相对于扫描目录），可多次指定，'
                             '支持 glob 通配符，如 --exclude "*.tmp" --exclude "build/*"')
    args = parser.parse_args()

    scan_dir = os.path.abspath(args.scan_dir)

    if not os.path.isdir(scan_dir):
        log(f"[ERROR] 目录不存在: {scan_dir}")
        sys.exit(1)

    exclude_patterns = args.exclude
    if exclude_patterns:
        log(f"[INFO] 排除规则: {exclude_patterns}")

    root_node, file_count, skipped_count = build_tree(scan_dir, exclude_patterns)

    log(f"[INFO] 共发现 {file_count} 个文件，排除 {skipped_count} 项")

    out_lines = []

    # 根下的直接文件：顶格输出（不带目录头）
    for fn, filepath in root_node['files']:
        log(f"[INFO] 处理文件: {filepath}")
        entry = process_file(filepath)
        if entry:
            filename, size, modified, url = entry
            out_lines.append(format_entry(filename, size, modified, url))

    # 顶层子目录：从缩进 0 开始
    for sub_name in sorted(root_node['dirs'].keys()):
        render_node(root_node['dirs'][sub_name], sub_name, 0, out_lines)

    result = "\n".join(out_lines)

    if args.output_file:
        try:
            with open(args.output_file, 'w', encoding='utf-8') as f:
                f.write(result)
                if result and not result.endswith('\n'):
                    f.write('\n')
            log(f"[INFO] 结果已写入: {args.output_file}")
        except Exception as e:
            log(f"[ERROR] 无法写入输出文件: {args.output_file} - {e}")
            sys.exit(1)
    else:
        sys.stdout.write(result)
        if result and not result.endswith('\n'):
            sys.stdout.write('\n')


if __name__ == '__main__':
    main()
