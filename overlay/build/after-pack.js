// electron-builder afterPack hook: ad-hoc signs unsigned builds.
//
// Packaging rewrites Electron's Info.plist, which invalidates the signature
// Electron shipped with. On Apple silicon a downloaded app with a broken
// signature is reported as "damaged" with no way to open it, whereas a valid
// ad-hoc signature gets the normal "Open Anyway" path. electron-builder 26
// does not accept "-" as an identity, so this signs explicitly.
//
// Runs only when CLUELESS_ADHOC_SIGN=1. With a real identity electron-builder
// signs after this hook and replaces the ad-hoc signature.
const { execFileSync } = require('child_process');
const path = require('path');

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== 'darwin' || process.env.CLUELESS_ADHOC_SIGN !== '1') return;

  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const entitlements = path.join(__dirname, '..', 'entitlements.mac.plist');
  const sign = (target, extra = []) => execFileSync('codesign', [
    '--force', '--sign', '-', '--options', 'runtime', '--timestamp=none', ...extra, target,
  ], { stdio: 'inherit' });

  // Inside-out: nested code first, so each enclosing seal covers final bytes.
  const machO = execFileSync('/bin/sh', ['-c',
    'find "$1/Contents/Resources" -type f \\( -perm -u+x -o -name "*.so" -o -name "*.dylib" \\) '
    + '-exec sh -c \'file -b "$1" | grep -q Mach-O && echo "$1"\' _ {} \\;', '_', app,
  ], { encoding: 'utf8' }).split('\n').filter((file) => file && !file.includes('.app/Contents/MacOS/'));
  // Executables need the entitlements too: under the hardened runtime, library
  // validation would stop the bundled python3 from loading its own extension
  // modules ("mapping process and mapped file have different Team IDs").
  for (const file of machO) sign(file, ['--entitlements', entitlements]);

  const helper = path.join(app, 'Contents', 'Resources', 'native', 'build', 'CluelessTap.app');
  sign(helper, ['--entitlements', entitlements]);

  const frameworks = path.join(app, 'Contents', 'Frameworks');
  const nested = execFileSync('/bin/sh', ['-c',
    'find "$1" -depth -name "*.app" -o -depth -name "*.framework"', '_', frameworks,
  ], { encoding: 'utf8' }).split('\n').filter(Boolean);
  for (const bundle of nested) sign(bundle, ['--entitlements', entitlements]);

  sign(app, ['--entitlements', entitlements]);
  execFileSync('codesign', ['--verify', '--deep', '--strict', app], { stdio: 'inherit' });
  console.log(`  • ad-hoc signed  ${machO.length} binaries in Resources, ${nested.length} framework bundles`);
};
