"""Exercise submission rules using real small PDFs and real ZIP archives."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'package.py'
spec = importlib.util.spec_from_file_location('package', SOURCE)
package = importlib.util.module_from_spec(spec) if SOURCE.exists() else None
if package:
    spec.loader.exec_module(package)


def small_pdf(path, pages):
    # A genuine PDF page tree and cross-reference table; no PDF-parser mocks.
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Count %d /Kids [%s] >>' % (pages, ' '.join(f'{i+3} 0 R' for i in range(pages)))).encode()]
    objects += [b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>'] * pages
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    start = len(data)
    data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    path.write_bytes(data)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(package, 'submission packager must exist')

    def test_rejects_placeholders_credentials_private_hosts_and_wrong_branches(self):
        urls = ['https://example.com/owner/repo/tree/main', 'https://github.com/OWNER/REPO/tree/main',
                'http://github.com/acme/platform/tree/main', 'https://token@github.com/acme/platform/tree/main',
                'https://127.0.0.1/repo/tree/main', 'https://localhost/repo/tree/main',
                'https://github.com/acme/platform/tree/master', 'https://github.com/acme/platform',
                'https://github.com/acme/platform/tree/main?token=secret']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                package.validate_url(url)

    def test_derives_public_git_repository_from_main_branch_url(self):
        self.assertEqual(package.validate_url('https://github.com/acme/platform/tree/main'), 'https://github.com/acme/platform.git')
        self.assertEqual(package.validate_url('https://gitlab.com/acme/project/-/tree/main'), 'https://gitlab.com/acme/project.git')

    def test_zip_contains_only_exact_submission_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            passport = root / 'input.pdf'
            small_pdf(passport, 4)
            url = 'https://github.com/acme/platform/tree/main'
            output = package.create_archive(url, 'Резван', passport, root / 'dist')
            self.assertEqual(output.name, 'Резван.zip')
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {'Ссылка.txt', 'Паспорт.pdf'})
                self.assertEqual(archive.read('Ссылка.txt').decode('utf-8'), url)
                self.assertEqual(archive.read('Паспорт.pdf'), passport.read_bytes())
                self.assertIsNone(archive.testzip())

    def test_rejects_fifth_page_and_malformed_pdf_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            passport = root / 'input.pdf'
            small_pdf(passport, 5)
            with self.assertRaises(ValueError):
                package.create_archive('https://github.com/acme/platform/tree/main', 'Резван', passport, root / 'dist')
            passport.write_text('not a PDF')
            with self.assertRaises(ValueError):
                package.create_archive('https://github.com/acme/platform/tree/main', 'Резван', passport, root / 'dist')
            self.assertFalse((root / 'dist' / 'Резван.zip').exists())

    def test_rejects_oversized_passport_and_filename_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            passport = root / 'input.pdf'
            with passport.open('wb') as stream:
                stream.truncate(15_000_001)
            with self.assertRaises(ValueError):
                package.create_archive('https://github.com/acme/platform/tree/main', 'Резван', passport, root / 'dist')
            small_pdf(passport, 1)
            for name in ('../escape', '/tmp/escape', 'Резван.zip', '..', 'trailing.'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    package.create_archive('https://github.com/acme/platform/tree/main', name, passport, root / 'dist')


if __name__ == '__main__':
    unittest.main()
