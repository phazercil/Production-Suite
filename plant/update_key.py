"""The public half of the key that signs update files (Admin > Updates).

Only update files signed with the matching private key install. The private key is kept off the server,
by whoever builds updates (tools/make_update.py). A signed update can carry a new public key here.
"""
PUBLIC_KEYS = ["hIgxp5j5ZXTdpafDCLIDhIRvMukDzXP3px4HPjEKoW8="]
