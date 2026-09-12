from nethub.credentials import read_credential


def test_no_credentials_directory_returns_none(monkeypatch):
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    assert read_credential('secret_key') is None


def test_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    assert read_credential('nope') is None


def test_empty_file_returns_none(tmp_path, monkeypatch):
    (tmp_path / 'secret_key').write_text('   \n')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    assert read_credential('secret_key') is None


def test_reads_and_strips_content(tmp_path, monkeypatch):
    (tmp_path / 'secret_key').write_text('  s3cr3t  \n')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    assert read_credential('secret_key') == 's3cr3t'
