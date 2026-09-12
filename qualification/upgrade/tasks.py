"""An enqueue-only control; the database stage starts no task worker."""

from django.tasks import task


@task
def current_task(value):
    raise AssertionError("the database upgrade stage must not execute application work")
