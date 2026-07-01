#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import os
from pathlib import Path

DEFAULT_TARGETS = Path('/testfiles/s3_targets.ini')
DEFAULT_AWS_DIR = Path.home() / '.aws'


def bool_value(value: str) -> bool:
    return value.strip().lower() in {'1', 'yes', 'true', 'on'}


def load_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return parser


def save_ini(path: Path, parser: configparser.ConfigParser, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        parser.write(fh)
    os.chmod(path, mode)


def backup(path: Path) -> None:
    if path.exists():
        stamp = dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')
        copy = path.with_name(path.name + f'.bak.{stamp}')
        copy.write_bytes(path.read_bytes())
        os.chmod(copy, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description='Write AWS CLI profiles from an S3 targets INI file.')
    parser.add_argument('provider_names', nargs='*', help='Optional provider sections or comma-separated provider lists')
    parser.add_argument('--targets', default=str(DEFAULT_TARGETS), help='INI file with provider credentials')
    parser.add_argument('--aws-dir', default=str(DEFAULT_AWS_DIR), help='AWS config directory, usually ~/.aws')
    args = parser.parse_args()

    only = {item.strip() for arg in args.provider_names for item in arg.split(',') if item.strip()}
    targets_path = Path(args.targets)
    aws_dir = Path(args.aws_dir).expanduser()
    creds_path = aws_dir / 'credentials'
    config_path = aws_dir / 'config'

    if not targets_path.exists():
        raise SystemExit(f'Target config not found: {targets_path}')

    targets = load_ini(targets_path)
    creds = load_ini(creds_path)
    config = load_ini(config_path)
    changed_profiles = []

    for section in targets.sections():
        if only and section not in only:
            continue
        if not bool_value(targets.get(section, 'enabled', fallback='false')):
            continue
        access_key = targets.get(section, 'access_key_id', fallback='').strip()
        secret_key = targets.get(section, 'secret_access_key', fallback='').strip()
        if not access_key or not secret_key or access_key.startswith('REPLACE_') or secret_key.startswith('REPLACE_'):
            continue
        profile = targets.get(section, 'profile', fallback='').strip() or section
        region = targets.get(section, 'region', fallback='us-east-1').strip() or 'us-east-1'

        if not creds.has_section(profile):
            creds.add_section(profile)
        creds.set(profile, 'aws_access_key_id', access_key)
        creds.set(profile, 'aws_secret_access_key', secret_key)
        token = targets.get(section, 'session_token', fallback='').strip()
        if token:
            creds.set(profile, 'aws_session_token', token)
        elif creds.has_option(profile, 'aws_session_token'):
            creds.remove_option(profile, 'aws_session_token')

        config_section = 'default' if profile == 'default' else f'profile {profile}'
        if not config.has_section(config_section):
            config.add_section(config_section)
        config.set(config_section, 'region', region)
        config.set(config_section, 'output', 'json')

        targets.set(section, 'profile', profile)
        if not targets.has_option(section, 'auth_mode'):
            targets.set(section, 'auth_mode', 'profile')
        else:
            targets.set(section, 'auth_mode', 'profile')
        changed_profiles.append(profile)

    backup(creds_path)
    backup(config_path)
    backup(targets_path)
    save_ini(creds_path, creds, 0o600)
    save_ini(config_path, config, 0o600)
    save_ini(targets_path, targets, 0o600)

    print('profiles_written=' + ','.join(changed_profiles))
    print('credentials_file=' + str(creds_path))
    print('config_file=' + str(config_path))
    print('targets_updated=' + str(targets_path))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
