import logging

import rollbar
from rollbar.logger import RollbarHandler

# Initialize Rollbar SDK with your server-side access token
rollbar.init(
    'ACCESS_TOKEN',
    environment='staging',
    handler='async',
)

# Set root logger to log DEBUG and above
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Report ERROR and above to Rollbar
rollbar_handler = RollbarHandler()
rollbar_handler.setLevel(logging.ERROR)

# Attach Rollbar handler to the root logger
logger.addHandler(rollbar_handler)
