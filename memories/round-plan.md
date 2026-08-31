Gap: The agent cannot reliably inspect task inputs packaged as ZIP/TAR archives, a common hard-task delivery format, without manually guessing shell commands and paths.
Change: Add a bounded archive inventory/extraction MCP tool that reads archives from materials/ and safely writes selected/all members to output/ or workspace.
Evidence: Existing document and image extraction already cover individual opaque inputs, while the current registry lacks archive handling; an executable archive-to-output workflow can directly distinguish this path.
