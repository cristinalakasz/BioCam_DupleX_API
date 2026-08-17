"""Layer 1 - reading the 3Brain assemblies' own metadata.

`_3Brain.Common` ships no XML documentation in this repository, so the
stimulation types it holds - `RectangularStimPulse`, `StimProperties` and the
protocol option classes - cannot be checked against documentation the way the
driver types can. They can, however, be read directly from the assembly.

Loading an assembly and reflecting over it needs the DLLs and pythonnet but
**not the instrument**: no USB call is made, no BioCAM is claimed, nothing is
activated. That makes this the one Layer 1 module that produces verified ground
truth on the development machine.

    python -m biocam.interop.reflect              # the stimulation types
    python -m biocam.interop.reflect --all-stim   # every type matching "stim"
    python -m biocam.interop.reflect Foo Bar      # named types

The output of this module is what `docs/api/stimulation-reference.md` records.
Regenerate rather than trusting a transcription.
"""

import sys

# Types worth printing by default: the ones Phase 2 builds on.
DEFAULT_TARGETS = (
    "RectangularStimPulse",
    "StimProperties",
    "StimPulse",
    "StimPoint",
    "StimPulseType",
    "IBioCamStim",
    "IStimProtocol",
    "IBioCamStimProtocolManager",
    "StimTrainProtocol",
    "StimEndPoint",
    "StimProtocolType",
    "StimStatus",
    "BioCamStimExternalEndPoint",
)


def _type_name(t) -> str:
    """Render a .NET type as something readable, generics included."""
    if t is None:
        return "void"
    name = t.Name
    if t.IsGenericType:
        args = ", ".join(_type_name(a) for a in t.GetGenericArguments())
        name = name.split("`")[0] + "<" + args + ">"
    return name


def _kind(t) -> str:
    if t.IsInterface:
        return "interface"
    if t.IsEnum:
        return "enum"
    if t.IsValueType:
        return "struct"
    return "class"


def describe(t) -> str:
    """Return a full public-surface description of a .NET type."""
    import System
    from System.Reflection import BindingFlags

    flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static

    out = [f"### {t.FullName}"]
    out.append(f"  assembly : {t.Assembly.GetName().Name}")
    out.append(f"  base     : {t.BaseType.FullName if t.BaseType else '-'}")
    out.append(f"  kind     : {_kind(t)}")

    if t.IsEnum:
        out.append("  values:")
        for name, value in zip(System.Enum.GetNames(t), System.Enum.GetValues(t)):
            out.append(f"    {name} = {System.Convert.ToInt64(value)}")
        return "\n".join(out) + "\n"

    ctors = t.GetConstructors()
    if ctors:
        out.append("  constructors:")
        for ctor in ctors:
            out.append(f"    .ctor({_params(ctor)})")

    # For an interface, Type.GetProperties/GetMethods return only the members
    # it declares itself - members inherited from base interfaces are NOT
    # included, and BindingFlags.FlattenHierarchy does not change that (it
    # affects statics). Since this tool's output is treated as ground truth,
    # an under-report reads as "the member does not exist", so base interfaces
    # are unioned in explicitly. For a class, GetProperties already walks the
    # base chain and t.GetInterfaces() adds nothing new.
    sources = [t] + (list(t.GetInterfaces()) if t.IsInterface else [])

    props = _union(sources, lambda s: s.GetProperties(flags))
    if props:
        out.append("  properties:")
        for p in sorted(props, key=lambda x: x.Name):
            access = ("get" if p.CanRead else "") + ("/set" if p.CanWrite else "")
            out.append(f"    {_type_name(p.PropertyType)} {p.Name}  ({access})")

    fields = _union(sources, lambda s: s.GetFields(flags))
    if fields:
        out.append("  fields:")
        for f in sorted(fields, key=lambda x: x.Name):
            const = ""
            if f.IsLiteral:
                try:
                    const = f" = {f.GetRawConstantValue()}"
                except Exception:  # noqa: BLE001 - a field we cannot read is not fatal
                    pass
            static = " [static]" if f.IsStatic else ""
            out.append(f"    {_type_name(f.FieldType)} {f.Name}{const}{static}")

    methods = [
        m for m in _union(sources, lambda s: s.GetMethods(flags))
        if not m.IsSpecialName
    ]
    if methods:
        out.append("  methods:")
        for m in sorted(methods, key=lambda x: x.Name):
            static = " [static]" if m.IsStatic else ""
            out.append(
                f"    {_type_name(m.ReturnType)} {m.Name}({_params(m)}){static}"
            )

    events = _union(sources, lambda s: s.GetEvents(flags))
    if events:
        out.append("  events:")
        for e in sorted(events, key=lambda x: x.Name):
            out.append(f"    {_type_name(e.EventHandlerType)} {e.Name}")

    return "\n".join(out) + "\n"


def _union(sources, get):
    """Collect members across a type and its base interfaces, de-duplicated.

    Keyed on name plus parameter shape so that genuine overloads survive while
    a member reachable through two base interfaces is printed once.
    """
    seen, members = set(), []
    for source in sources:
        for member in get(source):
            try:
                key = (member.Name, _params(member))
            except AttributeError:
                # Properties, fields and events have no GetParameters().
                key = (member.Name, None)
            if key in seen:
                continue
            seen.add(key)
            members.append(member)
    return members


def _params(method) -> str:
    parts = []
    for p in method.GetParameters():
        prefix = "out " if p.IsOut else "ref " if p.ParameterType.IsByRef else ""
        name = _type_name(p.ParameterType).rstrip("&")
        default = f" = {p.RawDefaultValue}" if p.IsOptional else ""
        parts.append(f"{prefix}{name} {p.Name}{default}")
    return ", ".join(parts)


def find_types(pattern=None, names=None):
    """Return the loaded 3Brain types matching a substring or an exact name."""
    import System

    assemblies = [
        a
        for a in System.AppDomain.CurrentDomain.GetAssemblies()
        if a.GetName().Name.startswith("3Brain")
    ]
    found = {}
    for assembly in assemblies:
        try:
            types = assembly.GetExportedTypes()
        except Exception:  # noqa: BLE001 - a partially-loadable assembly is skipped
            continue
        for t in types:
            if names is not None and t.Name in names:
                found[t.FullName] = t
            elif pattern is not None and pattern.lower() in t.Name.lower():
                found[t.FullName] = t
    return found


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # The obfuscated stack traces in these assemblies contain private-use
    # codepoints (U+E000 and up) that a cp1252 Windows console cannot encode -
    # printing one raises UnicodeEncodeError and hides the real error. Force
    # UTF-8 with a lossy fallback so output can never be the thing that fails.
    import io

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="backslashreplace"
        )

    from biocam.interop.device import load_assemblies

    load_assemblies()

    import System

    print("Loaded 3Brain assemblies:")
    for a in System.AppDomain.CurrentDomain.GetAssemblies():
        name = a.GetName()
        if name.Name.startswith("3Brain"):
            print(f"  {name.Name}  v{name.Version}")
    print()

    if "--all-stim" in argv:
        argv.remove("--all-stim")
        types = find_types(pattern="stim")
    elif argv:
        types = find_types(names=set(argv))
        missing = set(argv) - {t.Name for t in types.values()}
        for name in sorted(missing):
            print(f"### {name}: NOT FOUND\n")
    else:
        types = find_types(names=set(DEFAULT_TARGETS))

    for _, t in sorted(types.items()):
        print(describe(t))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
