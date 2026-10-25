"""How a post can fail. The class decides what happens to its Notion row."""


class PostFailed(Exception):
    """The post can never be processed (removed, private, not a post): the row is marked Failed."""


class TryLater(Exception):
    """A passing problem (network, a service down): the row stays Not started for the next Sync."""


class LoginNeeded(TryLater):
    """The site wants a logged-in session, or is limiting anonymous requests; the run skips the
    rest of that site's posts."""

    def __init__(self, site: str, message: str = ""):
        super().__init__(
            message or f"{site} needs a login: add its cookies to cookies.txt in the data folder"
        )
        self.site = site
