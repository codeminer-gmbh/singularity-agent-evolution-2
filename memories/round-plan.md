Cannot: exact-format solutions still over-accept near-valid inputs—the latest loss accepted signed `+1` where only stripped ASCII decimal digits were allowed; the missing boundary test is `revision=" +1 "` → reject.
Change: make the improvement/probe workflow require an executable acceptance-boundary table and negative-neighbor tests for every strict parser, then verify that policy through the public prompt path.
Why: repeated exam losses come from broadened parsing (signed decimals and double entity decoding), while more tools or generic freshness gates would not distinguish these failures.
