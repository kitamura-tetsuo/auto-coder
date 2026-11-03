"""Read the same self-contained evidence supplied at the semantic boundary."""

import json


def prompt_evidence(prompt):
    assert "Individual Evidence Envelope (transport version 1)" in prompt
    payload = prompt.rsplit("```json\n", 1)[1].split("\n```", 1)[0]
    # Independent test oracle: resolve definitions in dependency order, then
    # interpret the outgoing typed tree. No production codec is imported.
    envelope = json.loads(payload)
    assert envelope["version"] == 1
    prefix = "ie1:" + envelope["identity"] + ":"
    resolved = {}

    def tree(node):
        tag, value = node
        if tag == "r":
            return resolved[value]
        if tag == "v":
            return value
        if tag == "m":
            return {resolved[key]: tree(item) for key, item in value}
        assert tag in {"l", "t"}
        items = [tree(item) for item in value]
        return tuple(items) if tag == "t" else items

    pending = dict(enumerate(envelope["definitions"]))
    while pending:
        progress = False
        for index, definition in tuple(pending.items()):
            try:
                if definition[0] == "s":
                    text = definition[1]
                elif definition[0] == "c":
                    text = "".join(resolved[part] for part in definition[1])
                else:
                    assert definition[0] == "j"
                    value = tree(definition[2])
                    style = definition[1]
                    assert style in {"compact", "compact-sorted", "pretty", "pretty-sorted"}
                    options = {"ensure_ascii": False, "sort_keys": style.endswith("-sorted")}
                    if style.startswith("pretty"):
                        options["indent"] = 2
                    else:
                        options["separators"] = (",", ":")
                    text = json.dumps(value, **options)
            except KeyError:
                continue
            resolved[prefix + str(index)] = text
            del pending[index]
            progress = True
        assert progress, "Outgoing request cannot be independently resolved"
    return tree(envelope["root"])
