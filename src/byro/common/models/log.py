import datetime
import decimal
from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField
from django.db import models
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver
from django.utils.timezone import now

class ContentObjectManager(models.Manager):
    "An object manager that can handle filter(content_object=...)"
    def filter(self, *args, **kwargs):
        if 'content_object' in kwargs:
            content_object = kwargs.pop('content_object')
            kwargs['content_type'] = ContentType.objects.get_for_model(type(content_object))
            kwargs['object_id'] = content_object.pk
        return super().filter(*args, **kwargs)

class LogEntryManager(ContentObjectManager):
    "Manager that is linking the log chain on .create()"
    def create(self, *args, **kwargs):
        #prev_entry = LogEntry.objects.filter()
        return super().create(*args, **kwargs)


class LogEntry(models.Model):
    """A log entry for a change (or addition, or modification) in the database.

    :param content_object: The object that was modified (or added)
    :param datetime: Timestamp of the logged action
    :param user: The user that performed the action
    :param action_type: dot-namespaced type of the action, such as "byro.office.members.added"
    :param data: arbitrary additional data about the action.
      Well-known keys:
        * source: Actor/initiator of the change (a user or an automatic process). This must always be present. Will be generated from user attribute if set.
        * old: Value before the change
        * new: Value after the change
        * changes: dictionary of changes  key: (old_value, new_value)
    :param auth_hash: Authentication hash for the log chain. See docs/administrator/log-chain.rst
    :param auth_prev: `auth_hash` of the previous entry.
    :param auth_data: Authentication data for the log chain:
        * hash_ver: hash/algorithm version
        * nonce: A random nonce
        * data_mac: Message Authentication Code for the LogEntry.data attribute.
    """
    content_type = models.ForeignKey(ContentType, null=True, on_delete=models.SET_NULL)
    object_id = models.PositiveIntegerField(db_index=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    datetime = models.DateTimeField(default=now, db_index=True)
    user = models.ForeignKey(get_user_model(), null=True, on_delete=models.SET_NULL)

    action_type = models.CharField(max_length=255)
    data = JSONField(null=True)

    auth_hash = models.CharField(max_length=140, null=False, unique=True)
    auth_prev = models.ForeignKey('self', on_delete=models.PROTECT, related_name='auth_next', to_field='auth_hash', null=False, blank=False)
    auth_data = JSONField(null=False)

    objects = LogEntryManager()

    # FIXME Add pre-save signal, and, possibly, database-level protection against modification

    class Meta:
        ordering = ('-datetime', '-id')
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
            models.Index(fields=['action_type']),
        ]

    def delete(self, using=None, keep_parents=False):
        raise TypeError("Logs cannot be deleted.")

    def save(self, *args, **kwargs):
        if kwargs.get('update_fields', None) == ['auth_hash'] and self.auth_hash.startswith('random:'):
            self._OVERRIDE_SAVE = ['auth_hash']
        elif kwargs.get('update_fields', None) == ['auth_prev'] and self.auth_prev == 'undefined:0':
            self._OVERRIDE_SAVE = ['auth_prev']
        else:
            if getattr(self, 'pk', None):
                raise TypeError("Logs cannot be modified.")

            if not self.data:
                self.data = {}

            if self.user:
                self.data.setdefault('source', str(self.user))

            if not self.data.get('source'):
                raise ValueError("Need to provide at least user or data['source']")

        return super().save(*args, **kwargs)


@receiver(pre_save, sender=LogEntry)
def log_entry_pre_save(sender, instance, *args, **kwargs):
    if getattr(instance, '_OVERRIDE_SAVE', None) == ['auth_hash'] and instance.auth_hash.startswith('random:'):
        delattr(instance, '_OVERRIDE_SAVE')
    elif getattr(instance, '_OVERRIDE_SAVE', None) == ['auth_prev'] and instance.auth_prev == 'undefined:0':
        delattr(instance, '_OVERRIDE_SAVE')
    elif instance.pk:
        raise TypeError("Logs cannot be modified.")


@receiver(pre_delete, sender=LogEntry)
def log_entry_pre_delete(sender, instance, *args, **kwargs):
    if instance.pk:
        raise TypeError("Logs cannot be deleted.")


def flatten_objects(inobj, key_was=None):
    if isinstance(inobj, dict):
        return {k: flatten_objects(v, key_was=k) for k, v in inobj.items()}
    elif isinstance(inobj, (tuple, list)):
        return [flatten_objects(v) for v in inobj]
    elif isinstance(inobj, datetime.datetime):
        return inobj.strftime('%Y-%m-%d %H:%M:%S %Z')
    elif isinstance(inobj, datetime.date):
        return inobj.strftime('%Y-%m-%d')
    elif isinstance(inobj, decimal.Decimal) or (key_was == 'amount' and isinstance(inobj, (int, float))):
        return "{:.2f}".format(inobj)
    else:
        try:
            content_type = ContentType.objects.get_for_model(type(inobj))
            return {
                'object': content_type.name,
                'ref': (content_type.app_label, content_type.model, inobj.pk),
                'value': str(inobj)
            }
        except Exception:
            return str(inobj)


class LogTargetMixin:
    LOG_TARGET_BASE = None

    def log(self, context, action, user=None, **kwargs):
        if hasattr(context, 'request'):
            context = context.request

        user = user or getattr(context, 'user', None)

        if isinstance(context, str) and 'source' not in kwargs:
            kwargs['source'] = context

        if self.LOG_TARGET_BASE and action.startswith('.'):
            action = self.LOG_TARGET_BASE + action

        kwargs = flatten_objects(kwargs)

        LogEntry.objects.create(content_object=self, user=user, action_type=action, data=dict(kwargs))

    def log_entries(self):
        return LogEntry.objects.filter(content_object=self)


def log_call(action, log_on='retval'):
    def outer_decorator(f):
        @wraps(f)
        def decorator(*args, **kwargs):
            if 'user_or_context' not in kwargs:
                raise TypeError("You need to provide a 'user_or_context' named parameter which indicates the responsible user (a User model object), request (a View instance or HttpRequest object), or generic context (a str).")
            user_or_context = kwargs.pop('user_or_context')
            user = kwargs.pop('user', None)

            retval = f(*args, **kwargs)

            log_kwargs = {k: v for k, v in kwargs.items() if v}
            log_args = list(args)
            log_args = log_args[1:]   # Warning: we assume that args[0] is 'self'. Only works correctly with calls to bound methods
            if log_args:
                log_kwargs['_args'] = [v for v in log_args]

            if log_on == 'retval':
                retval.log(user_or_context, action, user=user, **log_kwargs)
            else:
                args[0].log(user_or_context, action, user=user, **log_kwargs)

            return retval

        return decorator

    return outer_decorator
