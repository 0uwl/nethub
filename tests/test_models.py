from nethub.models import User


def test_password_is_hashed_not_stored_plain():
    user = User(username='bob')
    user.set_password('correct-horse')
    assert user.password_hash != 'correct-horse'


def test_check_password_accepts_correct_and_rejects_wrong():
    user = User(username='bob')
    user.set_password('correct-horse')
    assert user.check_password('correct-horse') is True
    assert user.check_password('wrong') is False
