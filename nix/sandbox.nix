{
  writeShellApplication,
}:
writeShellApplication {
  name = "sandbox";
  text = ''
    exec ${../scripts/dev-sandbox.sh} "$@"
  '';
}
