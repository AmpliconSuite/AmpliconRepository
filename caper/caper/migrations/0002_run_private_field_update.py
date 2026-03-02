# Generated migration to update the Run model private field from BooleanField to CharField

from django.db import migrations, models


def convert_private_field_forward(apps, schema_editor):
    """
    Convert boolean private field values to new string format.
    - True -> 'private'
    - False -> 'public'
    """
    Run = apps.get_model('caper', 'Run')
    for run in Run.objects.all():
        if hasattr(run, 'private_new'):  # Only if we added the field
            if run.private is True:
                run.private_new = 'private'
            elif run.private is False:
                run.private_new = 'public'
            else:
                # Handle edge case
                run.private_new = 'private'
            run.save()


def convert_private_field_backward(apps, schema_editor):
    """
    Convert string visibility values back to boolean for rollback.
    - 'private' or 'hidden_public' -> True
    - 'public' -> False
    """
    Run = apps.get_model('caper', 'Run')
    for run in Run.objects.all():
        if hasattr(run, 'private'):
            visibility = run.private
            if visibility in ('private', 'hidden_public'):
                run.private = True
            else:  # 'public'
                run.private = False
            run.save()


class Migration(migrations.Migration):

    dependencies = [
        ('caper', '0001_initial'),
    ]

    operations = [
        # First, create the new field as nullable to avoid issues during migration
        migrations.AddField(
            model_name='run',
            name='private_new',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('private', 'Private'),
                    ('public', 'Public'),
                    ('hidden_public', 'Hidden Public - visible to anyone with the link')
                ],
                default='private',
                null=True
            ),
        ),
        # Data migration: Copy and convert boolean values to string values
        migrations.RunPython(
            code=convert_private_field_forward,
            reverse_code=convert_private_field_backward,
        ),
        # Remove the old private field
        migrations.RemoveField(
            model_name='run',
            name='private',
        ),
        # Rename the new field to private
        migrations.RenameField(
            model_name='run',
            old_name='private_new',
            new_name='private',
        ),
        # Make the field non-nullable
        migrations.AlterField(
            model_name='run',
            name='private',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('private', 'Private'),
                    ('public', 'Public'),
                    ('hidden_public', 'Hidden Public - visible to anyone with the link')
                ],
                default='private'
            ),
        ),
    ]


