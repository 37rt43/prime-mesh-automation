"""Secure local Prime 24.1 launcher. Importing this module starts no server."""
def launch_prime():
    import ansys.meshing.prime as prime
    return prime.launch_prime()
