"""Deployment settings an admin changes from the web UI (PLAN.md WS-16).

Only the three two-person rules so far. Each is read from its row whenever
an action asks, never cached, so turning a rule off releases whatever was
waiting on a second person, and turning it on applies to the next action.
Every change writes a `settings_audit` row in the same transaction.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .extensions import db
from .models import TWO_PERSON_RULES, Setting, SettingsAudit, User, _utcnow
from .web import admin_required

settings_bp = Blueprint('settings', __name__)

#: What each rule asks of an operator, as the settings page says it.
RULE_TEXT = {
    'two_person_artifacts': 'Artifacts: an upload waits until someone else publishes it, '
                            'and a delete until someone else confirms it.',
    'two_person_hostkeys': 'Host keys: a scan is confirmed by someone other than whoever '
                           'requested it, and removing a pin needs a second person.',
    'two_person_runs': 'Runs: every gate approval and every retry comes from someone other '
                       'than the submitter. Cancelling and declining cleanup never need '
                       'a second person.',
}

ON, OFF = 'on', 'off'


def enabled(key):
    row = db.session.get(Setting, key)
    return row is not None and row.value == ON


def applies(key, user):
    """Does the two-person rule `key` bind `user` right now? Admins are
    exempt; the records they leave say they acted as an admin."""
    return not user.is_admin and enabled(key)


def change(key, on, actor):
    """Set a rule, recording the change. Returns False if it already had
    that value, which writes nothing."""
    if key not in TWO_PERSON_RULES:
        raise ValueError(key)
    new = ON if on else OFF
    row = db.session.get(Setting, key)
    old = row.value if row is not None else None
    if (old or OFF) == new:
        return False
    if row is None:
        row = Setting(key=key)
        db.session.add(row)
    row.value, row.updated_by, row.updated_at = new, actor.id, _utcnow()
    db.session.add(SettingsAudit(key=key, old_value=old, new_value=new,
                                 changed_by=actor.id, changed_at=_utcnow()))
    return True


@settings_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def edit():
    if request.method == 'POST':
        changed = [key for key in TWO_PERSON_RULES
                   if change(key, request.form.get(key) == ON, current_user)]
        db.session.commit()
        flash(f'Changed: {", ".join(changed)}.' if changed else 'Nothing changed.',
              'success' if changed else 'info')
        return redirect(url_for('settings.edit'))
    history = (SettingsAudit.query
               .order_by(SettingsAudit.changed_at.desc(), SettingsAudit.id.desc()).all())
    return render_template(
        'pages/settings.html', rules=[(key, RULE_TEXT[key], enabled(key))
                                      for key in TWO_PERSON_RULES],
        history=history, users={u.id: u.username for u in User.query.all()},
    )
