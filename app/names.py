"""Every name the app uses for itself, in one place: what people see, and what it writes into Home Assistant."""

TITLE = 'Assist Request Tuner'        # the App store, page titles, README
SHORT = 'Request Tuner'               # the sidebar and the page header
SLUG = 'ar_tuner'                     # files, ids and keys it owns in Home Assistant
TECH = 'ar-tuner'                     # image, App and service names, user agent

# Files it manages in HA's config folder (relative to it).
SENTENCE_FILE = f'custom_sentences/en/{SLUG}.yaml'
BLUEPRINT_DIR = f'blueprints/automation/{SLUG}'
BLUEPRINT_FILE = 'voice_reply.yaml'
BLUEPRINT_PATH = f'{SLUG}/{BLUEPRINT_FILE}'   # how automations refer to it (relative to blueprints/automation)

# Automations it creates have ids starting with this.
AUTOMATION_PREFIX = f'{SLUG}_'
AUTOMATION_ID = rf'^{SLUG}_[a-z0-9_]+$'
MANAGED_BY = f'Managed by {TITLE}'

# Keys in the metadata of its custom sentences.
COMMENT_KEY = f'{SLUG}_comment'
SOURCE_KEY = f'{SLUG}_source'
HEADER = f'# {MANAGED_BY}; edits here are overwritten\n'

ENV_PREFIX = 'AR_TUNER_'
DB_FILE = f'/data/{SLUG}.db'
COOKIE = f'{SLUG}_token'
LOGGER = SLUG


def env(name):
    """AR_TUNER_<name> from the environment."""
    import os
    return os.getenv(ENV_PREFIX + name)


def managed_automation(automation_id):
    return isinstance(automation_id, str) and automation_id.startswith(AUTOMATION_PREFIX)
