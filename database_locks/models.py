from django.db import models
from django.utils import timezone


class Lock(models.Model):
    name = models.CharField(max_length=255, unique=True)
    locked_by = models.TextField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    @property
    def active(self):
        """Convenience for admin/debug only - compares against *this process's* clock, so it must
        not be used to decide lock acquisition (see DBLock.acquire, which evaluates expiry in SQL
        against the DB clock instead)."""
        return self.expires_at is not None and timezone.now() <= self.expires_at
