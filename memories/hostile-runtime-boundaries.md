# Hostile runtime boundaries

The probe and improvement roles share a conditional boundary pass in `prompts.py`. For contracts that admit subclasses, callbacks, locks, or numeric text, it directs the model to canonicalize scalar subclasses, avoid overridable built-in methods, keep caller code outside locks, pre-bound numeric conversion, and execute a focused hostile check. A prompt-construction assertion observed the protocol and each concrete requirement in both generated instruction strings.
