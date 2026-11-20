"""Use the operating system trust store for HTTPS when available."""


def enable_system_trust_store():
    try:
        import truststore
    except ImportError:
        return False

    truststore.inject_into_ssl()
    return True
