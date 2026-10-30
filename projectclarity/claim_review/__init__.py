"""Review extracted claims against source passages and track missing report fields.

Extraction proposes statements; a separate model request assesses whether their
quoted passages support them. Python then checks the replies and applies support
statuses. Modules here define review messages and response formats, group claims
for review, and track supported claims that have not reached the requested report.
A supported statement is not necessarily a complete report entry or a true fact
outside the cited source. Importing this package starts no review.
"""
