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


#: What `second_person_delete` found: go ahead and delete, the delete was
#: recorded as a request just now, or it was already this user's request.
GO, REQUESTED, WAITING = 'go', 'requested', 'waiting'


def second_person_delete(key, row, user):
    """The request-then-confirm step of a delete under two-person rule `key`
    (PLAN.md WS-16), shared by artifacts and host-key pins. `row` has `id`,
    `delete_requested_by` and `delete_requested_at`.

    `GO` when the rule does not bind `user` or someone else requested the
    delete: the caller deletes. Otherwise the delete is only a request:
    `REQUESTED` the first time, written as a conditional update so two
    people requesting at once make one request, and `WAITING` after. The
    caller adds its audit row and commits. Raises `RequestRaced` when
    someone else requested it between the read and the update: going ahead
    would turn this user's request into a confirmation they never saw.
    """
    if not applies(key, user) or row.delete_requested_by not in (None, user.id):
        return GO
    if row.delete_requested_by == user.id:
        return WAITING
    model = type(row)
    claimed = (db.session.query(model)
               .filter(model.id == row.id, model.delete_requested_by.is_(None))
               .update({'delete_requested_by': user.id, 'delete_requested_at': _utcnow()},
                       synchronize_session='fetch'))
    if claimed != 1:
        db.session.rollback()
        raise RequestRaced('Someone else requested this delete at the same moment, so '
                           'nothing was changed. Reload the page to confirm their request.')
    return REQUESTED


class RequestRaced(Exception):
    """Two people requested the same delete at once; the loser is told."""


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
        # An unticked checkbox is absent, so a form rendered before someone
        # else turned a rule on would turn it off again. Each rule carries
        # the value the page showed; if any moved since, change nothing.
        if any(request.form.get(f'shown_{key}') != (ON if enabled(key) else OFF)
               for key in TWO_PERSON_RULES):
            flash('The settings changed since you loaded this page. Nothing was '
                  'changed; check them again.')
            return redirect(url_for('settings.edit'))
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
