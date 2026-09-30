"""One router per area of the app. Each translates HTTP to a service call and back."""
from . import (
               corrections,
               diagnostics,
               experiences,
               exports,
               jobs,
               outreach,
               resumes,
               rewrites,
               settings,
)

ALL = [settings.router, resumes.router, experiences.router, jobs.router, rewrites.router,
       exports.router, corrections.router, outreach.router, diagnostics.router]
