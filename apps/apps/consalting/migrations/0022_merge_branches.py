# Generated manually to merge conflicting migration branches:
# - 0019_wazzupaccountconsalting_and_more
# - 0021_alter_inboundleadconsalting_phone_and_more

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0019_wazzupaccountconsalting_and_more'),
        ('consalting', '0020_whatsappmessageconsalting_content_uri_and_more'),
        ('consalting', '0021_alter_inboundleadconsalting_phone_and_more'),
    ]

    operations = [
    ]
