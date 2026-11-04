"""How a post can fail. The class decides what happens to its Notion row."""


class PostFailed(Exception):
    """The post can never be processed (removed, private, blocked): the row is marked Failed."""


class TryLater(Exception):
    """A passing problem (network, a service down): the row stays Not started for the next Sync.
    `needs_login` marks the problems a session in cookies.txt would solve; the gallery badges
    those posts "Needs login"."""

    needs_login = False


class LoginNeeded(TryLater):
    """The site wants a logged-in session, or is limiting anonymous requests (`needs_login`
    False); the run skips the rest of that site's posts."""

    def __init__(self, site: str, message: str = "", needs_login: bool = True):
        super().__init__(
            message or f"{site} needs a login: add its cookies to cookies.txt in the data folder"
        )
        self.site = site
        self.needs_login = needs_login

    def again(self) -> "LoginNeeded":
        """The same wall, for the next post of the site the run is skipping."""
        return LoginNeeded(self.site, str(self), self.needs_login)


class PostNeedsLogin(TryLater):
    """This one post needs a logged-in session (age-restricted, say); the site's other posts
    carry on."""

    needs_login = True
