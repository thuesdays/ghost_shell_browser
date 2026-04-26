"""ghost_shell.extensions — Chrome extension pool management.

Submodules:
    pool       — file-system layout + CRX/folder unpack + ID generation
    automation — flow steps for opening / interacting with extensions
"""

__author__ = "Mykola Kovhanko"
__email__  = "thuesdays@gmail.com"

from ghost_shell.extensions.pool import (
    POOL_DIR,
    pool_path,
    add_from_crx,
    add_from_unpacked_zip,
    install_from_cws,
    remove_from_pool,
    parse_manifest,
    extension_id_from_pubkey,
)

__all__ = [
    "POOL_DIR",
    "pool_path",
    "add_from_crx",
    "add_from_unpacked_zip",
    "install_from_cws",
    "remove_from_pool",
    "parse_manifest",
    "extension_id_from_pubkey",
]
