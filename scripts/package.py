#!/usr/bin/env python3
"""Create the two-file competition ZIP after PDF and anonymous main-branch checks."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import zipfile

PDF_LIMIT = 15_000_000  # Decimal MB is conservative for an unspecified MB limit.
ZIP_LIMIT = 18_000_000


def validate_url(url):
    if len(url) > 2048 or any(c.isspace() for c in url):
        raise ValueError('Укажите одну HTTPS-ссылку на публичную ветку main без пробелов.')
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
        raise ValueError('Нужна HTTPS-ссылка без credentials, query, fragment или нестандартного порта.')
    if '.' not in host or host.endswith(('.local', '.internal', '.test', '.invalid', '.localhost')) or host in ('example.com', 'example.org', 'example.net'):
        raise ValueError('Замените локальный адрес или placeholder реальным публичным Git-хостингом.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Укажите публичное DNS-имя Git-хостинга, а не IP-адрес.')
    path = parsed.path.rstrip('/')
    path = path.replace('/-/tree/main', '/tree/main')
    match = re.fullmatch(r'(/(?:[A-Za-z0-9_.~-]+/)+[A-Za-z0-9_.~-]+)/(?:tree|src)/main', path)
    if not match:
        raise ValueError('Ссылка должна вести на main: /owner/repository/tree/main (GitLab: /-/tree/main).')
    repository_path = match.group(1)
    placeholders = {'owner', 'repo', 'your-user', 'your-repo', 'username', 'repository', 'placeholder', 'public_main_url'}
    if any(part.lower() in placeholders for part in repository_path.split('/')) or '..' in repository_path.split('/'):
        raise ValueError('В ссылке остался placeholder; сначала опубликуйте реальный репозиторий.')
    if not repository_path.endswith('.git'):
        repository_path += '.git'
    return f'https://{parsed.netloc}{repository_path}'


def verify_public_main(url):
    repository = validate_url(url)
    if not shutil.which('git'):
        raise ValueError('Для анонимной проверки ветки main требуется git.')
    # Empty HOME/config disables stored credentials, aliases and URL rewrites.
    with tempfile.TemporaryDirectory(prefix='mtc-public-check-') as directory:
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        environment.update(HOME=directory, XDG_CONFIG_HOME=directory, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='false')
        try:
            result = subprocess.run(['git', '-c', 'credential.helper=', '-c', 'http.extraHeader=', 'ls-remote', '--exit-code', '--refs', repository, 'refs/heads/main'], cwd=directory, env=environment, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('Не удалось завершить анонимную проверку main за 45 секунд.') from exc
    if result.returncode or not re.fullmatch(r'[0-9a-f]{40,64}\s+refs/heads/main\s*', result.stdout):
        raise ValueError('Публичная ветка main не найдена без авторизации; проверьте публикацию и доступность сети.')
    return result.stdout.split()[0]


def pdf_pages(path):
    if path.stat().st_size > PDF_LIMIT:
        raise ValueError('Паспорт превышает 15 МБ.')
    with path.open('rb') as stream:
        if stream.read(5) != b'%PDF-':
            raise ValueError('Паспорт не является PDF.')
    if shutil.which('pdfinfo'):
        try:
            result = subprocess.run(['pdfinfo', str(path.resolve())], capture_output=True, text=True, timeout=20, env=dict(os.environ, LC_ALL='C'))
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Проверка PDF превысила 20 секунд.') from exc
        match = re.search(r'^Pages:\s+(\d+)\s*$', result.stdout, re.MULTILINE)
        if result.returncode or not match or re.search(r'^Encrypted:\s+yes', result.stdout, re.MULTILINE):
            raise ValueError('PDF поврежден или зашифрован.')
        pages = int(match.group(1))
    else:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError('Установите poppler-utils (pdfinfo) или pypdf в отдельное окружение упаковки.') from exc
        try:
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise ValueError('PDF зашифрован.')
            pages = len(reader.pages)
        except Exception as exc:
            raise ValueError('Не удалось прочитать незашифрованный PDF.') from exc
    if not 1 <= pages <= 4:
        raise ValueError(f'Паспорт должен содержать от 1 до 4 страниц; получено {pages}.')
    return pages


def create_archive(url, name, passport, output):
    """Validate local inputs and write ZIP atomically; CLI also verifies public main."""
    validate_url(url)
    if not re.fullmatch(r'[\w-][\w .-]{0,79}', name, re.UNICODE) or name.endswith(('.', ' ')):
        raise ValueError('Имя архива должно быть фамилией без пути или расширения; например sel.')
    if name.lower().endswith('.zip'):
        raise ValueError('Передайте имя без расширения .zip.')
    passport, output = Path(passport), Path(output)
    pdf_pages(passport)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'{name}.zip'
    with tempfile.NamedTemporaryFile(prefix='.submission-', suffix='.zip', dir=output, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('Ссылка.txt', url.encode('utf-8'))
            archive.write(passport, 'Паспорт.pdf')
        if temporary.stat().st_size > ZIP_LIMIT:
            raise ValueError('Архив превышает 18 МБ.')
        with zipfile.ZipFile(temporary) as archive:
            if archive.namelist() != ['Ссылка.txt', 'Паспорт.pdf'] or archive.testzip() is not None:
                raise ValueError('Контроль содержимого ZIP не пройден.')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-url', required=True, help='Реальная публичная HTTPS-ссылка на main')
    parser.add_argument('--name', default='sel', help='Имя архива без .zip')
    parser.add_argument('--passport', type=Path, default=Path('docs/passport/Паспорт.pdf'))
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    try:
        validate_url(args.repo_url)
        pages = pdf_pages(args.passport)
        commit = verify_public_main(args.repo_url)
        target = create_archive(args.repo_url, args.name, args.passport, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f'Упаковка остановлена: {exc}', file=sys.stderr)
        return 1
    print(f'{target}: {target.stat().st_size} bytes; паспорт {pages} стр.; публичный main {commit}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
