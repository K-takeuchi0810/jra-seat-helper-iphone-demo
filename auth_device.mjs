import {createPublicKey, verify} from 'node:crypto';
let raw='';
for await (const chunk of process.stdin) raw+=chunk;
try {
  const value=JSON.parse(raw);
  const key=createPublicKey({key:value.key,format:'jwk'});
  const valid=value.operation==='validate' || verify('sha256',Buffer.from(value.message,'utf8'),
    {key,dsaEncoding:'ieee-p1363'},Buffer.from(value.signature,'base64url'));
  process.stdout.write(JSON.stringify({valid}));
} catch {
  process.stdout.write(JSON.stringify({valid:false}));
}
