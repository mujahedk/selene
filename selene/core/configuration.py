# MIT License
#
# Copyright (c) 2015 Iakiv Kramarenko
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import atexit
import inspect
import itertools
import os
import time
import warnings
from types import MappingProxyType

import typing_extensions as typing
from selenium.common import WebDriverException
from selenium.webdriver.common.by import By
from typing_extensions import (
    Callable,
    Optional,
    Any,
    TypeVar,
    Dict,
    Literal,
    cast,
    Tuple,
)

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.options import BaseOptions
from selenium.webdriver.common.service import Service

from selene.common import fp, helpers
from selene.common.data_structures import persistent
from selene.common.fp import F
from selene.common.helpers import on_error_return_false

from selene.core.exceptions import TimeoutException

from selene.core.wait import Wait

E = TypeVar('E')


# TODO: consider moving to support.*
#       like support._logging.wait_with
def _build_local_driver_by_name_or_remote_by_url_and_options(
    config: Config,
) -> WebDriver:
    from selenium.webdriver import (
        ChromeOptions,
        EdgeOptions,
        Chrome,
        Firefox,
        Edge,
    )

    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.edge.service import Service as EdgeService  # type: ignore

    def build_chrome():
        return Chrome(
            service=config.driver_service or ChromeService(),
            options=config.driver_options,
        )

    def build_firefox():
        return Firefox(
            service=config.driver_service or FirefoxService(),
            options=config.driver_options,
        )

    def build_edge():
        return Edge(
            service=config.driver_service or EdgeService(),
            options=config.driver_options,
        )

    def build_remote_driver():
        from selenium.webdriver import Remote

        # TODO: consider guessing browserstack remote url
        #       if noticed 'bstack:options' in config.driver_options

        return Remote(
            command_executor=config.driver_remote_url,
            options=config.driver_options,
        )

    def build_appium_driver():
        try:
            from appium import webdriver
        except ImportError as error:
            raise ImportError(
                'Appium-Python-Client is not installed, '
                'run `pip install Appium-Python-Client`,'
                'or add and install dependency '
                'with your favorite dependency manager like poetry: '
                '`poetry add Appium-Python-Client`'
            ) from error

        # TODO: consider to add more smart guessing of options if not set...
        #       like if driver_name is set to 'appium'
        #       and driver_options is not set
        #       and the base_url is set to url of some web app
        #       then build appium driver options
        #       to run web test on mobile browser
        #       else if base_url is set to app path or url,
        #       parse app type and build corresponding appium driver options
        #       ...
        #       TODO: should we even rename base_url to app_url
        #             to cover both web and mobile? or just app?
        #             what about keeping both?
        #             but allowing to set only one of them at same moment?

        return webdriver.Remote(
            command_executor=(
                config.driver_remote_url
                if config.driver_remote_url
                else 'http://127.0.0.1:4723/wd/hub'
            ),
            options=config.driver_options,
        )

    return {  # type: ignore
        'chrome': build_chrome,
        'firefox': build_firefox,
        'edge': build_edge,
        'remote': build_remote_driver,
        'appium': build_appium_driver,
    }.get(
        'appium'
        if (
            config.driver_name == 'appium'
            or (
                config.driver_options
                and 'platformName' in config.driver_options.capabilities
                and config.driver_options.capabilities['platformName'].lower()
                in ['android', 'ios']
            )
        )
        else (
            'remote'
            if (config.driver_remote_url or config.driver_name == 'remote')
            # TODO: consider automatically detect installed browser if driver_name not set
            else (
                config.driver_name
                if config.driver_name
                else (
                    config.driver_options.capabilities['browserName']
                    if (
                        config.driver_options
                        and 'browserName' in config.driver_options.capabilities
                    )  # TODO: add one more check based on config.driver_service
                    else 'chrome'
                )
            )
        )
    )()


def _maybe_reset_driver_then_tune_window_and_get_with_base_url(config: Config):
    def get(url: Optional[str] = None) -> None:
        if (
            config._reset_not_alive_driver_on_get_url
            and config._executor.is_driver_set
            and config._executor.is_driver_managed
            and not config._executor.is_driver_alive
        ):
            # TODO: consider logging this reset
            #       so user will see it and decide to disable it
            config.driver = typing.cast(WebDriver, ...)

        driver = config.driver

        relative_or_absolute_url = url
        if relative_or_absolute_url is None:
            # force to init driver and open browser or app (for mobile)
            # _ = config.driver  # TODO: why not doing this in all cases?
            if not config.base_url:
                # do nothing more
                return
            if not config._get_base_url_on_open_with_no_args:
                # yet do nothing more
                return
            # proceed with adjusted relative url
            # to be concatenated with base url
            relative_or_absolute_url = ''

        # TODO: skip for mobile
        width = config.window_width
        height = config.window_height

        if width or height:
            if not (width and height):
                size = driver.get_window_size()
                width = width or size['width']
                height = height or size['height']

            driver.set_window_size(int(width), int(height))

        is_absolute = helpers.is_absolute_url(relative_or_absolute_url)
        base_url = config.base_url
        url = (
            relative_or_absolute_url
            if is_absolute
            else base_url + relative_or_absolute_url
        )

        # TODO: should we wrap it into wait? at least for logging?
        driver.get(url)

    return get


# TODO: should we do a complete Manager from it, not just executor,
#       by moving the corresponding logic from _ManagedDriverDescriptor?
# TODO: do we even need it? shouldn't we keep it more KISS?
class _DriverStrategiesExecutor:
    def __init__(self, config: Config):
        self.config = config

    @property
    def driver_instance(self) -> typing.Union[Optional[WebDriver], ...]:  # type: ignore
        return persistent.Field.value_from(self.config, 'driver')

    @property
    def is_driver_managed(self) -> bool:
        return not callable(self.driver_instance)

    def build_driver(self) -> WebDriver:
        return self.config.build_driver_strategy(self.config)

    # TODO: is_set or is_driver_set?
    #       kind of depends on class name.
    #       If named as ManagedDriver, then is_set
    #       If named as DriverManager, then is_driver_set
    @property
    def is_driver_set(self) -> bool:
        return self.config._is_driver_set_strategy(self.driver_instance)

    @property
    def is_driver_alive(self) -> bool:
        # TODO: should we check first if driver is set here?
        return self.config._is_driver_alive_strategy(self.driver_instance)  # type: ignore

    # TODO: this property suits better
    #       for something called as DriverManager not ManagedDriver ;)
    @property
    def teardown(self) -> Callable[[WebDriver], None]:
        return self.config._teardown_driver_strategy(self.config)

    # TODO: this property suits better
    #       for something called as DriverManager not ManagedDriver ;)
    def schedule_teardown(self, get_driver: Callable[[], WebDriver]) -> None:
        self.config._schedule_driver_teardown_strategy(self.config, get_driver)

    def get_url(self, url: Optional[str] = None) -> None:
        self.config._driver_get_url_strategy(self.config)(url)

    def save_screenshot(self, path: Optional[str] = None) -> Any:
        return self.config._save_screenshot_strategy(self.config, path)

    def save_page_source(self, path: Optional[str] = None) -> Any:
        return self.config._save_page_source_strategy(self.config, path)


# TODO: consider reusing config._executor inside this descriptor
class _ManagedDriverDescriptor:
    def __init__(
        self, *, default: typing.Union[Optional[WebDriver], ...] = ...  # type: ignore
    ):
        self.default = default
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self

        config = typing.cast(Config, instance)
        # Below...
        # we can't access driver via config.driver explicitly
        # or implicitly by calling other config.* methods,
        # because it will lead to recursion!!!

        driver_box = typing.cast(persistent.Box[WebDriver], getattr(config, self.name))
        if (
            driver_box.value is None
            or driver_box.value is ...
            or (
                # TODO: think on: if turned on, may slow down tests...
                #       especially when running remote tests...
                config.rebuild_not_alive_driver
                and not callable(driver_box.value)  # TODO: consider deprecating
                and not config._is_driver_alive_strategy(driver_box.value)
            )
        ):
            driver = config.build_driver_strategy(config)
            driver_box.value = driver
            config._schedule_driver_teardown_strategy(config, lambda: driver)

        value = driver_box.value
        if callable(value):
            # warnings.warn(
            #     'Providing driver as callable might be deprecated in future. '
            #     'Consider customizing driver management '
            #     'via other config.* options',
            #     FutureWarning,
            # )
            return value()

        return value

    def __set__(self, instance, value):
        config = typing.cast(Config, instance)

        if not hasattr(instance, self.name):
            # setting this attribute for the first time,
            # probably (TODO: probably or for sure?) in the __init__ method

            if isinstance(value, persistent.Box):
                # it's a boxed value,
                # probably passed implicitly via `persistent.replace`
                driver_box = value
            elif inspect.isdatadescriptor(value):
                # the value happened to be a descriptor
                # it's either object of this descriptor type (`type(self)`)
                # or custom provided descriptor during init new object
                if type(value) is type(self):
                    # we are processing this `self` descriptor as value
                    # so, instead of value, we should store `self.default`
                    driver_box = persistent.Box(self.default)
                else:
                    # somebody decided to provide his own descriptor object
                    # Heh:) It was a good try, but no ;P
                    raise TypeError(
                        'Providing custom descriptor objects on init '
                        'to customize driver management is not supported, '
                        'because it would be too limited... '
                        'You would be able to provide it only on init,'
                        'and use it only via attribute access,'
                        'without possibility to override value with `persistent.replace` '
                        'or `config.with_(**options_to_override)`. '
                        'If you want to use custom descriptor, '
                        'you have to subclass Config and provide your descriptor object'
                        'on class attributes definition level.'
                    )  # TODO: cover with tests
            else:
                # setting WebDriver instance directly on init
                driver_box = persistent.Box(value)

                # TODO: here we could remember somehow that driver was set manually
                #       do we need this?

                # currently passing driver as callable disables driver teardown
                if not callable(value):
                    config._schedule_driver_teardown_strategy(config, lambda: value)

            setattr(instance, self.name, driver_box)
        else:
            # setting WebDriver instance after init
            driver_box = getattr(instance, self.name)
            driver_box.value = value

            # currently passing driver as callable disables driver teardown
            if not callable(value):
                # TODO: should not we check value set above,
                #       wasn't the same as was in driver_box.value before?
                #       if yes, we might not want to add one more atexit handler
                config._schedule_driver_teardown_strategy(config, lambda: value)


@persistent.dataclass
class Config:
    """
    A one cross-cutting-concern-like object to group all options
    that might influence Selene behavior depending on context.
    For example, `config.timeout` is used in all "waiting" logic
    of Selene commands. And `config.base_url` is used
    in `browser.open(relative_url)` command.

    As option, the driver instance is also considered. Moreover, this config
    is not just config, but fully manages the driver lifecycle.
    Actually, the "driver manager" is a part of this config.

    While surfing through all available options, pay attention to terminology:

    - all options that have a `driver` word in their name
    are related to driver management, and they are connected in a specific way:)
        - read more on this under
        [`config.with_`][selene.core.configuration.Config.with_] doc section;)
    - all options that have a `strategy` word in their name directly influence
    the driver lifecycle in context of driver management.
    - !!! warning ""

            all options that are prefixed with `_` are considered "experimental"
            (their naming can be changed in the future,
            or even an option can be removed)

    Examples:
        Here's how you can build a driver with the instance of this config:

        >>> from selene import Config
        >>> config = Config()
        >>> driver = config.driver  # new instance, built on 1st access to `driver`
        >>> assert driver.name == 'chrome'

        Or pre-configuring the firefox driver:

        >>> from selene import Config
        >>> config = Config(driver_name='firefox')
        >>> driver = config.driver
        >>> assert driver.name == 'firefox'

        Or post-configuring the firefox driver:

        >>> from selene import Config
        >>> config = Config()
        >>> config.driver_name = 'firefox'
        >>> driver = config.driver
        >>> assert driver.name == 'firefox'

        Selene has already predefined shared instance of Config,
        so you can economize on lines of code;)...

        >>> from selene.support.shared import config
        >>> config.driver_name = 'firefox'
        >>> driver = config.driver
        >>> assert driver.name == 'firefox'

        Same shared Config instance is available as browser.config:

        >>> from selene import browser
        >>> browser.config.driver_name = 'firefox'
        >>> driver = browser.config.driver
        >>> assert driver.name == 'firefox'

        There is an alternative style of customizing config.
        The `config.option_name = value` is known in programming
        as "imperative programming" style. When you are creating
        a new Config from scratch, you are actually using
        a "declarative programming" style:

        >>> from selene import Config
        >>> my_config = Config(driver_name='firefox')
        >>> driver = my_config.driver
        >>> assert driver.name == 'firefox'

        Here is an alternative declarative style of
        customizing new config by copying existing:

        >>> from selene import browser
        >>> my_config = browser.config.with_(driver_name='firefox')
        >>> driver = my_config.driver
        >>> assert driver.name == 'firefox'
        >>> # AND...
        >>> assert driver is not browser.config.driver  # ;)
        >>> assert browser.config.driver.name == 'chrome'

        As you can see Selene config is closely related to the browser.
        Moreover, the same type of "declarative config copying" happens implicitly,
        when you apply "copying" to browser:

        >>> from selene import browser
        >>> second_browser = browser.with_(driver_name='firefox')
        >>> assert second_browser.config.driver.name == 'firefox'
        >>> # AND...
        >>> assert second_browser.config.driver is not browser.config.driver  # ;)
        >>> assert browser.config.driver.name == 'chrome'

        Moreover, if you need only a driver, you can have it
        via `browser.driver` shortcut, thus, completely hiding the config:

        >>> from selene import browser
        >>> second_browser = browser.with_(driver_name='firefox')
        >>> assert second_browser.driver.name == 'firefox'
        >>> # AND...
        >>> assert second_browser.driver is not browser.driver  # ;)
        >>> assert browser.driver.name == 'chrome'

        - such shortcut exists only for the `driver` option of config,
        not for other options like `timeout` or `base_url`.
        More nuances of `browser` behavior find in its docs;).

        And here are some more examples of customizing config
        for common test automation use cases...

        Scenario: "Run locally in headless Chrome"

        >>> from selene import browser
        >>> from selenium import webdriver
        >>>
        >>> options = webdriver.ChromeOptions()
        >>> options.add_argument('--headless')
        >>> # additional options:
        >>> options.add_argument('--no-sandbox')
        >>> options.add_argument('--disable-gpu')
        >>> options.add_argument('--disable-notifications')
        >>> options.add_argument('--disable-extensions')
        >>> options.add_argument('--disable-infobars')
        >>> options.add_argument('--enable-automation')
        >>> options.add_argument('--disable-dev-shm-usage')
        >>> options.add_argument('--disable-setuid-sandbox')
        >>> browser.config.driver_options = options

        Scenario: "Run remotely on Selenoid"

        >>> import os
        >>> from selene import browser
        >>> from selenium import webdriver
        >>>
        >>> options = webdriver.ChromeOptions()
        >>> options.browser_version = '100.0'
        >>> options.set_capability(
        >>>     'selenoid:options',
        >>>     {
        >>>         'screenResolution': '1920x1080x24',
        >>>         'enableVNC': True,
        >>>         'enableVideo': True,
        >>>         'enableLog': True,
        >>>     },
        >>> )
        >>> browser.config.driver_options = options
        >>> browser.config.driver_remote_url = (
        >>>     f'https://{os.getenv("LOGIN")}:{os.getenv("PASSWORD")}@'
        >>>     f'selenoid.autotests.cloud/wd/hub'
        >>> )

        Scenario: "Run remotely on BrowserStack in iOS Safari"

        >>> import os
        >>> from selene import browser
        >>> from selenium.webdriver.common.options import ArgOptions
        >>>
        >>> options = ArgOptions()
        >>> options.set_capability(
        >>>     'bstack:options',
        >>>     {
        >>>         'deviceName': 'iPhone 14 Pro Max',
        >>>         # 'browserName': 'safari',  # default for iPhone
        >>>         'userName': os.getenv('BROWSERSTACK_USERNAME'),
        >>>         'accessKey': os.getenv('BROWSERSTACK_ACCESS_KEY'),
        >>>     },
        >>> )
        >>> browser.config.driver_options = options
        >>> browser.config.driver_remote_url = 'http://hub.browserstack.com/wd/hub'

        Scenario: "Run locally in Android Chrome"

        >>> from selene import browser
        >>> from appium.options.common import AppiumOptions
        >>>
        >>> mobile_options = AppiumOptions()
        >>> mobile_options.new_command_timeout = 60
        >>> # Mandatory, also tells Selene to build Appium driver:
        >>> mobile_options.platform_name = 'android'
        >>> mobile_options.set_capability('browserName', 'chrome')
        >>>
        >>> browser.config.driver_options = mobile_options
        >>> # Not mandatory, because it is the default value:
        >>> # browser.config.driver_remote_url = 'http://127.0.0.1:4723/wd/hub'

        Scenario: "Run locally in Android App"

        >>> from selene import browser
        >>> from appium.options.android import UiAutomator2Options
        >>>
        >>> android_options = UiAutomator2Options()
        >>> android_options.new_command_timeout = 60
        >>> android_options.app = 'wikipedia-alpha-universal-release.apk'
        >>> android_options.app_wait_activity = 'org.wikipedia.*'
        >>>
        >>> browser.config.driver_options = android_options

        Scenario: "Run remotely in Android App on BrowserStack"

        >>> import os
        >>> from selene import browser
        >>> from appium.options.android import UiAutomator2Options
        >>>
        >>> options = UiAutomator2Options()
        >>> options.app = 'bs://c700ce60cf13ae8ed97705a55b8e022f13c5827c'
        >>> options.set_capability(
        >>>     'bstack:options',
        >>>     {
        >>>         'deviceName': 'Google Pixel 7',
        >>>         'userName': os.getenv('BROWSERSTACK_USERNAME'),
        >>>         'accessKey': os.getenv('BROWSERSTACK_ACCESS_KEY'),
        >>>     },
        >>> )
        >>> browser.config.driver_options = options
        >>> browser.config.driver_remote_url = 'http://hub.browserstack.com/wd/hub'

    By having config options that influences Selene behavior,
    like `config.timeout` and `config.base_url`,
    – together with complete "driver management",
    we definitely break SRP principle... In the name of Good:D. Kind of;).

    All this makes it far from being a simple options data class...
    – yet kept as one "class for everything" to keep things easier to use,
    especially taking into account some historical reasons of Selene design,
    that was influenced a lot by the Selenide from Java world.
    As a result sometimes options are not consistent with each other,
    when we speak about different contexts of their usage.
    For example, this same config,
    once customized with `config.driver_options = UiAutomator2Options()`,
    will result in mobile driver built, but then all other web-related options,
    for example, a `config.base_url` will be not relevant.
    Some of them will be ignored, while some of them,
    for example js-related, like `config.set_value_by_js`,
    will break the code execution (JavaScript does not work in mobile apps).
    In an ideal world, we would have to split this config into several ones,
    starting BaseConfig and continuing with WebConfig, MobileConfig, etc.
    Yet, we have what we have. This complicates things a bit,
    especially for us, contributors of Selene,
    but makes easier for newbies in a lot of "harder" cases,
    like customizing same shared browser instance for multi-platform test runs,
    when we have one test that works for all platforms.
    Thus, we allow to do "harder" tasks easier for "less experienced" users.
    Again, such "easiness" does not mean "simplicity" for us, contributors,
    and also for advanced Selene users,
    who want to customize things in a lot of ways
    and have them easier to support on a long run.
    But for now, let's keep it as is, considered as a trade-off.
    """

    # TODO: should it be more as a strategy builder (i.e. curried function)?
    #       i.e.
    #           build_driver_strategy: Callable[
    #             [Config], Callable[[], WebDriver]
    #           ]
    #       for consistency with some other options...
    build_driver_strategy: Callable[[Config], WebDriver] = (
        _build_local_driver_by_name_or_remote_by_url_and_options
    )
    """A factory to build a driver instance based on this config instance.
    The driver built with this factory will be available via `config.driver`.
    Hence, you can't use `config.driver` directly inside this factory,
    because it may lead to recursion.

    The default factory builds:

    - either a local driver by value specified in `config.driver_name`
    - or a local driver by browserName capability specified in `config.driver_options`
    - or remote driver by value specified in `config.driver_remote_url`
    - or mobile driver according to `config.driver_options` capabilities
    """

    # TODO: isn't this option too much?
    #       having it, we have to keep driver descriptor definition
    #       after this option definition,
    #       that is pretty tightly coupled...
    #       heh, but maybe we definitely have to keep it defined
    #       after all "strategy" options...
    # Currently we don't use the power of get_driver being callable...
    # It would work even if we pass simply driver instance...
    # Should we simplify things? Or keep it as is with get_driver?
    _schedule_driver_teardown_strategy: Callable[
        [Config, Callable[[], WebDriver]],
        typing.Union[None, typing.Any],
    ] = lambda config, get_driver: atexit.register(
        lambda: config._teardown_driver_strategy(config)(get_driver())
    )
    """Defines when drier will be teardown.
    Is supposed to use config._teardown_driver_strategy under the hood.

    By default, it's registered as an atexit handler.
    """

    # TODO: since it's curried, shouldn't we rename it driver_teardown_strategy?
    _teardown_driver_strategy: Callable[[Config], Callable[[WebDriver], None]] = (
        lambda config: lambda driver: (
            driver.quit()
            if not config.hold_driver_at_exit
            and config._is_driver_set_strategy(driver)
            and config._is_driver_alive_strategy(driver)
            else None
        )
    )
    """Defines how driver will be teardown.

    By default it quits the driver if it's alive and not asked to be held at exit
    via `config.hold_driver_at_exit`.
    """

    # TODO: should we make it private so far?
    # TODO: shouldn't it be config-based?
    _is_driver_set_strategy: Callable[[Optional[WebDriver]], bool] = lambda driver: (
        driver is not ... and driver is not None
    )
    """Defines how to check if driver is set, and so defines how to "unset"
    or "reset" it.
    """

    # TODO: should we make it private so far?
    _is_driver_alive_strategy: Callable[[WebDriver], bool] = lambda driver: (
        # on_error_return_false(lambda: driver.title is not None)
        (driver.service.process is not None and driver.service.process.poll() is None)
        if hasattr(driver, 'service')
        else on_error_return_false(lambda: driver.window_handles is not None)
    )
    """Defines the logic of checking driver for being alive.

    Is supposed to be used in context of triggering automatic driver rebuild,
    depending on context.
    """

    driver_options: Optional[BaseOptions] = None
    """Individual browser options to be used on building a driver.

    Examples:
        Can be used instead of `config.driver_name` to tell Selene
        which driver to build, e.g. just specifying

        >>> from selene import browser
        >>> from selenium import webdriver
        >>>
        >>> browser.config.driver_options = webdriver.FirefoxOptions()`

        – will tell Selene to build a Firefox driver.

        But usually you want something more specific,
        like specifying to run a browser in headless more:

        >>> from selene import browser
        >>> from selenium import webdriver
        >>>
        >>> options = webdriver.ChromeOptions()
        >>> options.add_argument('--headless')
        >>> browser.config.driver_options = options
    """

    driver_service: Optional[Service] = None
    """Service instance for managing the starting and stopping of the driver.
    Might be useful, for example, for customizing driver executable path,
    if you want to use a custom driver executable instead of the one,
    downloaded by Selenium Manager automatically.
    """

    # Probably, more precise and technically correct name and signature would be:
    #     driver_remote_connection: Optional[Union[str, RemoteConnection]] = None
    # But we decided to keep it more simple and user-friendly
    # in context of the majority of use cases when we just need to pass a URL:
    # for appium and remote cases
    driver_remote_url: Optional[str] = None
    """A URL to be used as remote server address to instantiate a RemoteConnection
    to be used by RemoteWebDriver to connect to the remote server.

    Also known as `command_executor`,
    when passing on init: `driver = remote.WebDriver(command_executor=HERE)`.
    Currently we name it and type hint as URL,
    but if you pass a RemoteConnection object,
    it will work same way as in Selenium WebDriver.
    """

    # TODO: consider typing as Optional[Literal['chrome', 'firefox', 'edge', 'appium']]
    # TODO: consider setting to None or ... by default,
    #       and pick up by factory any installed browser in a system
    driver_name: Optional[str] = None
    """A desired name of the driver to build by `config.build_driver_strategy`.

    If not set (i.e. set to None, that is a current default value),
    the 'chrome' driver will be used by default.

    It is ignored by default `config.build_driver_strategy`
    if `config.driver_remote_url` is set.

    If you are going to provide your desired driver options
    via `config.driver_options`,
    then Selene will try to guess the corresponding driver name
    based on the options you provided. I.e. no need to provide both:

    ```python
    config.driver_name = 'chrome'
    config.driver_options = ChromeOptions()
    ```

    It's enough to provide only the options:

    ```python
    config.driver_options = ChromeOptions()
    ```

    GIVEN set to any of: 'chrome', 'firefox', 'edge',<br>
    AND config.driver is left unset (default value is ...),<br>
    THEN default config.build_driver_strategy will automatically install drivers<br>
    AND build webdriver instance for you<br>
    AND this config will store the instance in config.driver<br>
    """

    # TODO: finalize the name of this option and consider making public
    _override_driver_with_all_driver_like_options: bool = True
    """Controls whether driver will be deep copied
    with `config.driver_name`, `config.driver_remote_url`,
    and so for any other `config.*driver*` option.
    when customizing config via `config.with_(**options_to_override)`.

    Examples:
        Building 2 drivers with implicit deep copy of driver storage:

        >>> chrome_config = Config(
        >>>     driver_name='chrome',
        >>>     timeout=10.0,
        >>>     base_url='https://autotest.how',
        >>> )
        >>> chrome = chrome_config.driver
        >>> firefox_config = chrome_config.with_(driver_name='firefox')
        >>> firefox = firefox_config.driver
        >>> assert firefox is not chrome

        Building 2 drivers with explicit deep copy of driver storage [1]:

        >>> chrome_config = Config(
        >>>     driver_name='chrome',
        >>>     timeout=10.0,
        >>>     base_url='https://autotest.how',
        >>>     _override_driver_with_all_driver_like_options=False,
        >>> )
        >>> chrome = chrome_config.driver
        >>> firefox_config = chrome_config.with_(driver_name='firefox', driver=...)
        >>> firefox = firefox_config.driver
        >>> assert firefox is not chrome

        Building 2 drivers with explicit deep copy of driver storage [2]:

        >>> chrome_config = Config(
        >>>     driver_name='chrome',
        >>>     timeout=10.0,
        >>>     base_url='https://autotest.how',
        >>> )
        >>> chrome_config._override_driver_with_all_driver_like_options = False
        >>> chrome = chrome_config.driver
        >>> firefox_config = chrome_config.with_(name='firefox', driver=...)
        >>> firefox = firefox_config.driver
        >>> assert firefox is not chrome

        Building 1 driver because driver storage was not copied:

        >>> chrome_config = Config(
        >>>     driver_name='chrome',
        >>>     timeout=10.0,
        >>>     base_url='https://autotest.how',
        >>> )
        >>> chrome_config._override_driver_with_all_driver_like_options = False
        >>> chrome = chrome_config.driver
        >>> firefox_config = chrome_config.with_(name='firefox')
        >>> firefox = firefox_config.driver
        >>> assert firefox is chrome  # o_O ;)
    """

    # TODO: consider to deprecate because might confuse in case of Appium usage
    @property
    def browser_name(self) -> Optional[str]:
        return self.driver_name

    # TODO: consider to deprecate because might confuse in case of Appium usage
    @browser_name.setter
    def browser_name(self, value: str):
        self.driver_name = value

    # TODO: do we need it?
    # quit_last_driver_on_reset: bool = False
    # """Controls whether driver will be automatically quit at reset of config.driver"""

    hold_driver_at_exit: bool = False
    """Controls whether driver will be automatically quit at process exit or not.

    Will not take much effect on Chrome
    for 4.5.0 < selenium versions <= 4.8.3 < ?.?.?,
    Because for some reason, Selenium of such versions kills driver by himself,
    regardless of what Selene thinks about it:D
    """

    @property
    def hold_browser_open(self) -> bool:
        warnings.warn(
            'Was deprecated because "browser" term '
            'is not relevant to mobile context. '
            'Use `config.hold_driver_at_exit` instead',
            DeprecationWarning,
        )
        return self.hold_driver_at_exit

    @hold_browser_open.setter
    def hold_browser_open(self, value: bool):
        warnings.warn(
            'Was deprecated because "browser" term '
            'is not relevant to mobile context. '
            'Use `config.hold_driver_at_exit = ...` instead',
            DeprecationWarning,
        )
        self.hold_driver_at_exit = value

    # TODO: maybe like this:
    #         _driver_get_url_strategy: Callable[[Config, str], None] = get_driver
    #       or should we implement same "decorator-like" style
    #       for other config-based strategies too?
    # TODO: refactor to inline definition with lambda style
    _driver_get_url_strategy: Callable[
        [Config],
        Callable[[Optional[str]], None],
    ] = _maybe_reset_driver_then_tune_window_and_get_with_base_url
    """Defines how to get url with driver depending on other options,
    like config.base_url.

    Is used inside `browser.open(relative_or_absolute_url)`,
    and defines its behavior correspondingly.
    """

    # TODO: should we use `rebuild` term instead of `reset`?
    #       to be consistent with `rebuild_not_alive_driver`...
    #       Technically, we are not explicitly rebuilding it with this option,
    #       we do reset it by setting to `...`. But then in same `get_url`,
    #       on first access it will be rebuilt automatically.
    #       But shouldn't we use a goal-oriented term, not impl-oriented?
    #       The goal is actually to rebuild driver on get url!
    _reset_not_alive_driver_on_get_url: bool = True
    """Controls whether driver should be automatically reset and, thus,
    forced to be rebuilt, if it was noticed as not alive (e.g. after quit or crash)
    on next call to `config.driver.get(url)`
    (via `config._driver_get_url_strategy`).

    Does not work if `config.driver` was set manually to `Callable[[], WebDriver]`.

    Is a more "week" option than `config.rebuild_not_alive_driver`,
    that is disabled by default,
    that forces to rebuild driver on any next access.
    """

    rebuild_not_alive_driver: bool = False
    """Controls whether driver should be automatically rebuilt
    when on next call to config.driver
    it was noticed as not alive (e.g. after quit or crash).

    May slow down your tests if running against remote Selenium server,
    e.g. Grid or selenoid, because of additional request to check
    if driver is alive per each driver action.

    Does not work if `config.driver` was set manually to `Callable[[], WebDriver]`.

    Is a more "strong" option than `config._reset_not_alive_driver_on_get_url`,
    (enabled by default), that schedules rebuilding driver
    on next access only inside "get url" logic.
    """

    # # TODO: why it is not working as attribute + post init?
    # _executor: _DriverStrategiesExecutor = typing.cast(_DriverStrategiesExecutor, ...)
    # def __post_init__(self):
    #     self._executor = _DriverStrategiesExecutor(self)
    @property
    def _executor(self):
        return _DriverStrategiesExecutor(self)

    # TODO: consider allowing to provide a Descriptor as a value
    #       by inheritance like:
    #           class MyConfig(Config):
    #              driver: WebDriver = HERE_DriverDescriptor(...)
    #       and then `MyConfig(driver=...)` will work as expected
    # TODO: should we accept a callable here to bypass build_driver_strategy logic?
    #       currently we do... we don't show it explicitly...
    #       but the valid type is Union[WebDriver, Callable[[], WebDriver]]
    #       so... should we do it?
    #       why not just use config.build_driver_strategy for same?
    #       there is the difference though...
    #       the driver factory is only used as a driver builder,
    #       it does not cover other stages of driver lifecycle,
    #       like teardown...
    #       but if we provide a callable instance to driver,
    #       then it will just substitute the whole lifecycle
    # TODO: consider renaming the class of *Descriptor.
    #       It can be named as ManagedDriverDescriptor, because this is what it is.
    #       But since we have another object named as _managed_driver,
    #       it might be confusing... so maybe DriverManagerDescriptor?
    #       or DriverLifeCycleDescriptor?
    #       or is it ok to have similar names? We still have a Descriptor suffix...
    driver: WebDriver = _ManagedDriverDescriptor(default=...)  # type: ignore
    """A driver instance with lifecycle managed by this config special options
    depending on their values and customization of this attribute.

    Once driver-definition-related options are set
    (like `config.driver_options`, `config.driver_remote_url`),
    the driver will be built on first access to this attribute.
    Thus, to build the driver with Selene,
    you simply call `config.driver` for the first time,
    and usually the simplest way to access it
    – is through either `browser.config.driver` or even `browser.driver` shortcut.
    Moreover, usually you don't do this explicitly,
    but rather use `browser.open(url)` to build a driver and open a page.

    Scenarios:
        GIVEN unset, i.e. equals to default `...` or `None`,<br>
        WHEN accessed first time (e.g. via config.driver)<br>
        THEN it will be set to the instance built by `config.build_driver_strategy`.<br>

        GIVEN set manually to an existing driver instance,
              like: `config.driver = Chrome()`<br>
        THEN it will be reused as it is on any next access<br>
        WHEN reset to `...` OR `None`<br>
        THEN will be rebuilt by `config.build_driver_strategy`<br>

        GIVEN set manually to an existing driver instance (not callable),
              like: `config.driver = Chrome()`<br>
        AND at some point of time the driver is not alive
            like crashed or quit<br>
        AND `config._reset_not_alive_driver_on_get_url` is set to `True`,
            that is default<br>
        WHEN driver.get(url) is requested under the hood
             like at `browser.open(url)`<br>
        THEN config.driver will be reset to `...`<br>
        AND thus will be rebuilt by `config.build_driver_strategy`<br>

        GIVEN set manually to a callable that returns WebDriver instance
              (currently marked with FutureWarning, so might be deprecated)<br>
        WHEN accessed fist time<br>
        AND any next time<br>
        THEN will call the callable and return the result<br>

        GIVEN unset or set manually to not callable<br>
        AND `config.hold_driver_at_exit` is set to `False` (that is default)<br>
        WHEN the process exits<br>
        THEN driver will be quit.<br>
    """

    timeout: float = 4
    """A default timeout for all Selene waiting that happens under the hood
    of the majority of Selene commands and assertions.
    """

    poll_during_waits: int = 100
    """A fake option, not currently used in Selene waiting:)"""

    # --- Web-specific options ---
    # TODO: should we pass here None?
    #       and use "not None" as _get_base_url_on_open_with_no_args=True?
    # TODO: should we rename it to app_url? or even just app?
    #       and use it as app capability for mobile?
    #       if not set in driver_options...
    base_url: str = ''
    """A base url to be used when opening a page with relative url.

    Examples:
        Instead of duplicating the same base url in all your tests:

        >>> from selene import browser
        >>> browser.open('https://mywebapp.xyz/signin')
        >>> ...
        >>> browser.open('https://mywebapp.xyz/signup')
        >>> ...
        >>> browser.open('https://mywebapp.xyz/profile')
        >>> ...

        You can set it once in your config and then just use relative urls:

        >>> from selene import browser
        >>> browser.config.base_url = 'https://mywebapp.xyz'
        >>> browser.open('/signin')
        >>> ...
        >>> browser.open('/signup')
        >>> ...
        >>> browser.open('/profile')
        >>> ...
    """
    # TODO: when adding driver_get_url_strategy
    #       should we rename it to get_base_url_when_relative_url_is_missed?
    #       should we use driver term in the name?
    _get_base_url_on_open_with_no_args: bool = False
    """A flag to indicate whether to use `config.base_url`
    when opening a page with `browser.open()` command without arguments.

    If you call `browser.open()` without arguments,
    it will just force the driver to be built and real browser to be opened.

    Even if you have set `config.base_url`, to reuse it in your tests on `browser.open`,
    you have to specify the url explicitly, like `browser.open('/')`
    or at least `browser.open('')`.

    But there are cases where you would like to load base url on `browser.open()`,
    for example in context of cross-platform testing. Then you use this option;)
    See example at
    https://github.com/yashaka/selene/blob/master/examples/run_cross_platform
    """
    window_width: Optional[int] = None
    """If set, will be used to set the window width on next call to `browser.open(url)`.
    """
    window_height: Optional[int] = None
    """If set, will be used to set the window height on next call
    to `browser.open(url)`.
    """
    log_outer_html_on_failure: bool = False
    """If set to True, will log outer html of the element on failure
    of any Selene command.

    Is disabled by default, because:

    - it might add too much of noise to the logs
    - will not work on mobile app tests because under the hood - uses JavaScript
    """
    set_value_by_js: bool = False
    """A flag to indicate whether to use JavaScript to set value of an element
    on `element.set_value(value)` for purposes of speeding up the test execution,
    or as a workaround when default selenium-based implementation does not work.
    """
    type_by_js: bool = False
    """A flag to indicate whether to use JavaScript to type text to an element
    on `element.type(text)` for purposes of speeding up the test execution,
    or as a workaround when default selenium-based implementation does not work.
    """
    click_by_js: bool = False
    """A flag to indicate whether to use JavaScript to click on element
    via `element.click()`, usually, as a workaround,
    when default selenium-based implementation does not work.
    """
    drag_and_drop_by_js: bool = False
    """A flag to indicate whether to use JavaScript to drag and drop a web
    element via `source_element.drag_and_drop_to(target_element)`, usually,
    as a workaround, when default selenium-based implementation does not work.
    """
    # TODO: consider wait_with_js_for_element_interactable
    #       should we add also (or instead?)
    #       config.is_element_interactable_strategy?
    wait_for_no_overlap_found_by_js: bool = False
    """A flag to indicate whether to use JavaScript to detect overlapping elements
    and wait for them to disappear, when calling commands like `element.type(text)`.
    It is needed because Selenium does not support overlapping elements detection
    on any command except `click`. Hence, when you call `click` on an element,
    and there is some overlay on top of it
    (e.g. for the sake of indicating "loading in progress"),
    that is going to disappear after some time,
    then Selenium will detect such overlap,
    that tells Selene to wait for it to disappear.
    But for any other command (double_click, context_click, type, etc.)
    Selenium will not and so Selene will not wait.
    Hence, if you want to wait in such cases, turn on this option.
    Just keep in mind, that it will work only for web tests, not mobile.
    """
    # TODO: should not we explicitly provide a context in name?
    #       because now it looks like the filter will be added always
    #       even here: browser.all('.item').locate()
    _match_only_visible_elements_texts: bool = True
    """A flag to indicate whether to filter out all elements for visibility
    on applying Selene conditions that match elements collection texts
    (usually in Selene assertions vis `should` method calls).
    """  # todo: document example
    _match_only_visible_elements_size: bool = False
    """A flag to indicate whether to filter out all elements for visibility
    on applying Selene conditions that match elements collection size
    (usually in Selene assertions vis `should` method calls).

    It is set to False by default for backward compatibility reasons.
    """  # todo: document example
    # todo: decide on naming: ignore_case vs match_ignoring_case?
    #       ignore_case conciser but
    #       - not consistent with other match_* options
    #       - does not explicitly tell that relates only to matching
    #         * but... it should be kind of obvious that can be used only in context of matching
    #         * and actually it's consistent with have.*.ignore_case conditions
    #       - ! it will confuse in the following context:
    #         > browser.with(ignore_case=True).element(by.text('submit')).click()
    #         as we might start think that the search by text will be done ignoring case
    #         but it will not :(
    # todo: consider documenting the major list of conditions that are affected by this option
    _match_ignoring_case: bool = False
    # TODO: find the best name...
    #       compare:
    #       > _placeholders_to_match_elements
    #       > _placeholders_to_list_globs
    #       > _placeholders_for_list_globs
    #       > _placeholders_for_list_globs_to_match
    _placeholders_to_match_elements: Dict[
        Literal['zero_or_one', 'exactly_one', 'one_or_more', 'zero_or_more'], Any
    ] = cast(dict, MappingProxyType({}))
    """A dict of default placeholders to be used among values passed to Selene
    collection conditions like `have._texts_like(*values)`. Such values then can
    be considered as a list globbing pattern, where a defined placeholder will

    match the corresponding to placeholder type number of ANY elements.

    The default list globbing placeholders are:

    - `[{...}]` matches **zero or one** item of any text in the list
    - `{...}` matches **exactly one** item of any text in the list
    - `...` matches one **or more** items of any text in the list
    - `[...]` matches **zero** or more items of any text in the list

    Thus, using this option you can redefine them. Assuming, you don't like, that
    `...` matches "one or more" and want it to match "exactly one" instead, then
    you can set the following defaults:

    ```python
    from selene import browser

    ...
    # GIVEN default placeholders
    browser.all('.number0-9').should(have._texts_like([{...}], 0, {...}, 2, ...))

    # WHEN
    browser.config._placeholders_to_match_elements = {
        'zero_or_one': '[...]',
        'exactly_one': '...',
        'one_or_more': '(...,)',
        'zero_or_more': '[(...,)]',
    }

    # THEN
    browser.all('.number0-9').should(have._texts_like([...], 0, ..., 2, (...,)))
    ```

    All globbing placeholders can be mixed in the same list of expected item values
    in any order.
    * """

    # todo: consider automatically add [] around the attribute=value
    #       including quotes around value with spaces
    #       automatically convert text=... to xpath by text
    #       find more inspirations from:
    #       https://playwright.dev/docs/other-locators
    #       and
    #       https://playwright.dev/docs/locators
    #       and
    #       https://github.com/microsoft/playwright/releases/tag/v1.27.0
    selector_to_by_strategy: Callable[[str], Tuple[str, str]] = lambda selector: (
        (By.XPATH, selector)
        if (
            selector.startswith('/')
            or selector.startswith('./')
            or selector.startswith('..')
            or selector.startswith('(')
            or selector.startswith('*/')
        )
        else (By.CSS_SELECTOR, selector)
    )
    """A strategy to convert a selector string to a Selenium By type of selector,
    that is a 2-dimension tuple of selector type and selector value.

    Can be useful to define custom selectors to be used on building common Selene
    entities like `browser.element(selector)` or `browser.all(selector)`.

    You can find a simple example of such strategy definition in the default value
    of this option. Here goes a smarter example of building a custom strategy on top
    of the default one, that will automatically convert a "one word" selector string
    to the `[data-testid=<WORD>]` css selector:

    ```python
    # tests/conftest.py
    import re
    import pytest
    import selene
    from selene.common.helpers import _HTML_TAGS

    @pytest.fixture(scope='function', autouse=True)
    def browser_management():
        selene.browser.config.selector_to_by_strategy = lambda selector: (
            # wrap into default strategy
            selene.browser.config.selector_to_by_strategy(
                # detected testid
                f'[data-testid={selector}]'
                if re.match(
                    # word_with_dashes_underscores_or_numbers
                    r'^[a-zA-Z_\d\-]+$',
                    selector,
                )
                and selector not in _HTML_TAGS
                else selector
            )
        )

        yield

        selene.browser.quit()
    ```
    """

    def _selector_or_by_to_by(
        self,
        selector_or_by: str | Tuple[str, str],
        /,
    ) -> Tuple[str, str]:
        return (
            self.selector_to_by_strategy(selector_or_by)  # noqa
            if isinstance(selector_or_by, str)
            else selector_or_by
        )

    # TODO: better name? now technically it's not a decorator but decorator_builder...
    # or decorator_factory...
    # yet in python they call it just "decorator with args" or "decorator with params"
    # so technically we are correct naming it simply _wait_decorator
    # by type hint end users yet will see the real signature
    # and hence guess its "builder-like" nature
    # yet... should we for verbosity distinguish options
    # that a decorator factories from options that are simple decorators?
    # maybe better time to decide on this will be once we have more such options :p
    # TODO: What about config.wait_decorator_strategy?
    #       or even config.build_wait_decorator_strategy?
    _wait_decorator: Callable[[Wait[E]], Callable[[F], F]] = lambda w: lambda f: f
    """Is used when performing any element command and assertion (i.e. should)
    Hence, can be used to log corresponding commands with waits,
    and integrate with something like allure reporting;)

    Yet prefixed with underscore, indicating that method is experimental,
    and so can be renamed or change its type signature, etc.

    The default value of this option just does nothing.

    See example of implementing and setting a custom wait decorator
    at https://github.com/yashaka/selene/blob/master/examples/log_all_selene_commands_with_wait.py

    Examples:
        And here is how you can use some predefined wait decorators in Selene,
        for example, to configure logging Selene commands to Allure report:

        >>> from selene.support.shared import browser
        >>> from selene import support
        >>> import allure_commons
        >>>
        >>> browser.config._wait_decorator = support._logging.wait_with(
        >>>   context=allure_commons._allure.StepContext
        >>> )
    """

    _disable_wait_decorator_on_get_query: bool = True
    """A flag controlling whether to disable wait decorator on calls
    like entity.get(query.*), turned on by default,
    because queries usually serve as auxiliary methods
    to get some intermediate information,
    and not influence significantly the test flow,
    that is usually reported and logged via wait decorator. Hence, by default,
    we disable wait decoration for queries,
    removing redundant noise from potential logging
    and speeding up the test execution for some specific cases
    like working with nested Frames
    (for more see warning at
    [_FrameContext.element][selene.web._elements._FrameContext.element]).
    """

    # TODO: why we name it as hook_* why not handle_* ?
    #       what would be proper style?
    hook_wait_failure: Optional[Callable[[TimeoutException], Exception]] = None
    """A handler for all exceptions, thrown on failed waiting for timeout.
    Should process the original exception and rethrow it or the modified one.
    """

    reports_folder: str = os.path.join(
        os.path.expanduser('~'),
        '.selene',
        'screenshots',
        str(round(time.time() * 1000)),
    )
    """A folder to save screenshots and page sources on failure."""
    save_screenshot_on_failure: bool = True
    """A flag to indicate whether to save screenshot on failure or not.
    If saved, will be also logged to the console on failure.
    """
    save_page_source_on_failure: bool = True
    """A flag to indicate whether to save page source on failure or not.
    If saved, will be also logged to the console on failure.
    """
    # TODO: consider making public
    _counter: itertools.count = itertools.count(start=int(round(time.time() * 1000)))
    """A counter, currently used for incrementing screenshot and page source names"""
    last_screenshot: Optional[str] = None
    last_page_source: Optional[str] = None
    # TODO: is a _strategy suffix a good naming convention in this context?
    #       maybe yes, because we yet accept config in it...
    #       so we expect it to be a Strategy of some bigger Context
    # TODO: why the return type is Any? shouldn't it be a string of path?
    _save_screenshot_strategy: Callable[[Config, Optional[str]], Any] = (
        lambda config, path=None: fp.thread(  # type: ignore
            path,
            lambda path: (
                config._generate_filename(suffix='.png') if path is None else path
            ),
            lambda path: (
                os.path.join(path, f'{next(config._counter)}.png')
                if path and not path.lower().endswith('.png')
                else path
            ),
            fp.do(
                fp.pipe(  # type: ignore
                    os.path.dirname,
                    lambda folder: (
                        os.makedirs(folder)
                        if folder and not os.path.exists(folder)
                        else ...
                    ),
                )
            ),
            fp.do(
                lambda path: (
                    warnings.warn(
                        'name used for saved screenshot does not match file '
                        'type. It should end with an `.png` extension',
                        UserWarning,
                    )
                    if not path.lower().endswith('.png')
                    else ...
                )
            ),
            lambda path: (path if config.driver.get_screenshot_as_file(path) else None),
            fp.do(
                lambda path: setattr(config, 'last_screenshot', path)
            ),  # On refactor>rename, we may miss it here :( better would be like:
            #  setattr(config, config.__class__.last_screenshot.name, path)
            #  but currently .name will return '__boxed_last_screenshot' :(
            #  think on how we can resolve this...
        )
    )
    """Defines a strategy for saving a screenshot.

    The default strategy saves a screenshot to a file,
    and stores the path to `config.last_screenshot`.
    """

    _save_page_source_strategy: Callable[[Config, Optional[str]], Any] = (
        lambda config, path=None: fp.thread(  # type: ignore
            path,
            lambda path: (
                config._generate_filename(suffix='.html') if path is None else path
            ),
            lambda path: (
                os.path.join(path, f'{next(config._counter)}.html')
                if path and not path.lower().endswith('.html')
                else path
            ),
            fp.do(
                fp.pipe(  # type: ignore
                    os.path.dirname,
                    lambda folder: (
                        os.makedirs(folder)
                        if folder and not os.path.exists(folder)
                        else ...
                    ),
                )
            ),
            fp.do(
                lambda path: (
                    warnings.warn(
                        'name used for saved page source does not match file '
                        'type. It should end with an `.html` extension',
                        UserWarning,
                    )
                    if not path.lower().endswith('.html')
                    else ...
                )
            ),
            lambda path: (path, config.driver.page_source),
            fp.do(lambda path_and_source: fp.write_silently(*path_and_source)),
            lambda path_and_source: path_and_source[0],
            fp.do(
                lambda path: setattr(config, 'last_page_source', path)
            ),  # On refactor>rename, we may miss it here :( better would be like:
            #  setattr(config, config.__class__.last_screenshot.name, path)
            #  but currently .name will return '__boxed_last_screenshot' :(
            #  think on how we can resolve this...
        )
    )
    """Defines a strategy for saving a page source on failure.

    The default strategy saves a page_source to a file,
    and stores the path to `config.last_page_source`.
    """

    # TODO: consider adding option to disable persistence of all not-overridden options
    #       or marking some of them as not persistent
    #       (i.e. unbind some of them keeping the previous value set)
    def with_(self, **options_to_override) -> Config:
        """

        Returns (Config):
            A new config with overridden options that were specified as arguments.

            All other config options will be shallow-copied
            from the current config.
            Those other options that are of immutable types,
            like `int` - will be also copied by reference,
            i.e. in a truly shallow way.

        Parameters:
            **options_to_override (Any):
                options to override in the new config.

                Technically "override" here means:
                "deep copy option storage and update its value to the specified one".
                All other option storages will be:
                "shallow copied from the current config".

                If `driver_name` is among `options_to_override`,
                and `driver` is not among them,
                and `self._override_driver_with_all_driver_like_options` is True,
                then `driver` will be implicitly added to the options to override,
                i.e. `with_(driver_name='firefox')` will be equivalent
                to `with_(driver_name='firefox', driver=...)`.
                The latter gives a readable and concise shortcut
                to spawn more than one browser:

                ```python
                from selene import Config

                config = Config(timeout=10.0, base_url='https://autotest.how')
                chrome = config.driver  # chrome is default browser
                firefox_config = config.with_(driver_name='firefox')
                firefox = firefox_config.driver
                edge_config = config.with_(driver_name='edge')
                edge = edge_config.driver
                ```

                Same logic applies to `remote_url`,
                and all other config.*driver* options.
        """
        options = (
            {'driver': ..., **options_to_override}
            if (
                self._override_driver_with_all_driver_like_options
                and 'driver' not in options_to_override
                and any('driver' in key for key in options_to_override)
            )
            else options_to_override
        )
        return persistent.replace(self, **options)

    # TODO: should we make it and similar – true private over protected?
    def _format_path_as_uri(self, path):
        """Converts a local file path to a URI that can be clicked in most editors and browsers."""
        prefix = 'file://'
        if os.name == 'nt':  # Windows specific
            # Replace backslashes with forward slashes and prepend with 'file://'
            return f"{prefix}{path.replace(os.sep, '/')}"
        else:
            # Unix-based paths
            return f"{prefix}{path}"

    def _generate_filename(self, prefix='', suffix=''):
        path = self.reports_folder
        next_id = next(self._counter)
        filename = f'{prefix}{next_id}{suffix}'
        file = os.path.join(path, f'{filename}')

        folder = os.path.dirname(file)
        if not os.path.exists(folder) and folder:
            os.makedirs(folder)

        return file

    # TODO: consider moving this injection to the WaitingEntity.wait method
    #       to build Wait object instead of config.wait
    def _inject_screenshot_and_page_source_pre_hooks(self, hook):
        # TODO: consider moving hooks to class methods accepting config as argument
        #       or refactor somehow to eliminate all times defining hook fns
        def save_and_log_screenshot(error: TimeoutException) -> Exception:
            # todo: consider changing _save_screenshot_strategy to be Either-like
            #       > path, maybe_failure = self._save_screenshot_strategy(self)
            #       but then the f.thread that is used in its impl
            #       should become monad-friendly,
            #       with some kind of .map/.bind under the hood
            #       and propagating error to the end if happened
            path, maybe_failure = fp._either(
                self._save_screenshot_strategy, or_=WebDriverException
            )(self)
            return TimeoutException(
                error.msg
                # todo: should we just skip logging screenshot at all when failure?
                #       here and below for page_source
                + '\nScreenshot: '
                + (
                    self._format_path_as_uri(path)
                    if not maybe_failure
                    else 'cannot be saved because of: {name}: {message}'.format(
                        name=maybe_failure.__class__.__name__,
                        message=getattr(maybe_failure, "msg", str(maybe_failure)),
                    )
                )
            )

        def save_and_log_page_source(error: TimeoutException) -> Exception:
            filename = (
                # TODO: this dependency to last_screenshot might lead to code,
                #       when wrong last_page_source name is taken
                self.last_screenshot.replace('.png', '.html')
                if self.last_screenshot
                else self._generate_filename(suffix='.html')
            )

            path, maybe_failure = fp._either(
                self._save_page_source_strategy, or_=WebDriverException
            )(self, filename)
            return TimeoutException(
                error.msg
                + '\nPageSource: '
                + (
                    self._format_path_as_uri(path)
                    if not maybe_failure
                    else 'cannot be saved because of: {name}: {message}'.format(
                        name=maybe_failure.__class__.__name__,
                        message=getattr(maybe_failure, "msg", str(maybe_failure)),
                    )
                )
            )

        return fp.pipe(
            save_and_log_screenshot if self.save_screenshot_on_failure else None,
            save_and_log_page_source if self.save_page_source_on_failure else None,
            hook,
        )

    # TODO: maybe here wait_factory would be better name?
    #       yes, it's also a strategy, but completely not connected with other
    #       driver lifecycle strategies
    _build_wait_strategy: Callable[[Config], Callable[[E], Wait[E]]] = (
        lambda config: lambda entity: Wait(
            entity,
            at_most=config.timeout,
            or_fail_with=config._inject_screenshot_and_page_source_pre_hooks(
                config.hook_wait_failure
                # if not config.log_outer_html_on_failure
                # # optionally inject outer html logging:
                # # currently commented... seems like this is partially a bad idea...
                # # the actual implementation based on config.log_outer_html_on_failure
                # # will live in actual entities implementation
                # # if we put it here, we would have to predict all cases inforward,
                # # that we can't... so better to leave it to the entities
                # # the idea to have it here – can be reasoned by the fact that
                # # the config.log_outer_html_on_failure lives here:)
                # # yet... probably in future it would be good
                # # to break this General Config into core.Config, web.Config, mobile.Config
                # # then we could consider moving such hook adjustments to config...
                # # it would be good, because currently we will have remove ability
                # # to log outer html for core.Element in case it is used for web...
                # else fp.pipe(
                #     lambda error: fp.thread(
                #         fp._either(
                #             lambda entity: entity.locate(), or_=WebDriverException
                #         )(entity),
                #         lambda either_webelement_or_failure: (
                #             error
                #             if either_webelement_or_failure[1] is not None
                #             else TimeoutException(
                #                 f'{error.msg}\n'
                #                 f'Actual webelement: {either_webelement_or_failure[0].get_attribute("outerHTML")}'
                #             )
                #         ),
                #     ),
                #     config.hook_wait_failure,
                # )
            ),
            _decorator=config._wait_decorator,
        )
    )
    """A strategy for building a Wait object based on other config options
    like `config.timeout`, `config.hook_wait_failure`, `config._wait_decorator`, etc.
    """

    # TODO: we definitely not need it inside something called Config,
    #       especially "base interface like config
    #       consider refactor to wait_factory as configurable config property
    # TODO: should we move it config._executor.wait(entity) ?
    def wait(self, entity: E) -> Wait[E]:
        return self._build_wait_strategy(self)(entity)

    def _wait(self, entity: E) -> Wait[E]:
        return self.wait(entity)
