Gap: the agent can only issue bounded HTTP GETs, so it cannot call JSON/form REST endpoints that require POST, PUT, PATCH, DELETE, request headers, or a request body.
Change: add a bounded `http_request` workspace tool that supports those methods, validated headers and text/JSON bodies, and returns the same safe response summary as GET.
Priority: interactive web/API tasks are a distinct class from the many local-format readers already present, and this extends the live-network capability rather than duplicating an existing parser.
