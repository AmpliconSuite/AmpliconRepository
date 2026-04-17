import ssl

from django.core.mail.backends.smtp import EmailBackend
from django.core.mail.utils import DNS_NAME


class SSLCompatEmailBackend(EmailBackend):
    """
    SMTP email backend compatible with Python 3.10 and 3.12+.

    Django 4.0.x passes deprecated `keyfile` and `certfile` kwargs to
    smtplib.SMTP.starttls(), which were removed in Python 3.12. This
    backend replaces those with an ssl.SSLContext, which works on both.
    """

    def _make_ssl_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        if self.ssl_keyfile and self.ssl_certfile:
            context.load_cert_chain(certfile=self.ssl_certfile, keyfile=self.ssl_keyfile)
        return context

    def open(self):
        if self.connection:
            return False

        connection_params = {"local_hostname": DNS_NAME.get_fqdn()}
        if self.timeout is not None:
            connection_params["timeout"] = self.timeout
        if self.use_ssl:
            connection_params["context"] = self._make_ssl_context()

        try:
            self.connection = self.connection_class(self.host, self.port, **connection_params)
            if not self.use_ssl and self.use_tls:
                self.connection.ehlo()
                self.connection.starttls(context=self._make_ssl_context())
                self.connection.ehlo()
            if self.username and self.password:
                self.connection.login(self.username, self.password)
            return True
        except OSError:
            if not self.fail_silently:
                raise
