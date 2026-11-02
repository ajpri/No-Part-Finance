"""Recovery forms and fragment handling, using no database or email provider."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import tests
from flask import Flask, render_template_string


class RecoveryFrontendTests(unittest.TestCase):
    def render(self, mode, recoveryEnabled=True, fields=None, message=""):
        application = Flask(__name__)
        with application.app_context():
            return render_template_string(Path("authentication.html").read_text(), mode=mode, title="Recovery test", csrfToken="synthetic-csrf", registrationEnabled=False, recoveryEnabled=recoveryEnabled, version="26.11", fields=fields or {}, message=message)

    def testRecoveryLinkPolicyAndNativeFormFields(self):
        self.assertNotIn('href="/forgot-password"', self.render("signIn", False))
        self.assertIn('href="/forgot-password"', self.render("signIn"))
        forgot = self.render("forgotPassword", fields={"username": "<script>username</script>"}, message="If eligible, a link will be sent.")
        self.assertIn('method="post" action="/forgot-password"', forgot)
        self.assertIn('name="csrfToken" value="synthetic-csrf"', forgot)
        self.assertIn('name="username" type="text" inputmode="email"', forgot)
        self.assertIn("&lt;script&gt;username&lt;/script&gt;", forgot)
        self.assertNotIn("<script>username</script>", forgot)
        self.assertIn("If eligible, a link will be sent.", forgot)
        reset = self.render("resetPassword", fields={"token": "a" * 43}, message="Passwords do not match.")
        self.assertIn('method="post" action="/reset-password"', reset)
        self.assertIn('name="token" value="' + "a" * 43 + '"', reset)
        self.assertIn('name="newPassword" type="password"', reset)
        self.assertIn('name="confirmation" type="password"', reset)
        self.assertIn("Passwords do not match.", reset)
        complete = self.render("recoveryComplete", message="Password reset. Sign in again.")
        self.assertIn('href="/sign-in"', complete)
        self.assertNotIn("<form", complete)

    def testResetFragmentTransferRemovalAndNoTokenRefresh(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Set NODE_BINARY for reset fragment handler checks")
        source = Path("authentication.js").read_text()
        function = "function preparePasswordReset" + source.split("function preparePasswordReset", 1)[1].split("preparePasswordReset();", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
let form, location, replaced;
const document={querySelector(){return form;}};
const history={replaceState(state,title,path){replaced=path;location.hash="";}};
function reset(fragment,postedToken=""){
 location={hash:fragment,pathname:"/reset-password",search:""};replaced=null;
 const token={value:postedToken},message={textContent:"",hidden:true},submit={disabled:false};
 form={querySelector(key){return {"[data-reset-token]":token,"[data-reset-token-message]":message,"[data-reset-submit]":submit}[key];},addEventListener(key,callback){this[key]=callback;}};
 preparePasswordReset();return {token,message,submit};
}
async function check(){
 const secret="A".repeat(42)+"_";
 let controls=reset("#"+secret);
 assert.equal(controls.token.value,secret);assert.equal(replaced,"/reset-password");
 assert.equal(location.hash,"");assert.equal(controls.message.hidden,true);assert.equal(controls.submit.disabled,false);
 let prevented=false;form.submit({preventDefault(){prevented=true;}});assert.equal(prevented,false);
 controls=reset("");assert.equal(controls.token.value,"");assert.equal(controls.submit.disabled,true);assert.equal(controls.message.hidden,false);
 assert.equal(replaced,null,"Refresh cannot reconstruct the removed secret");
 controls=reset("",secret);assert.equal(controls.token.value,secret);assert.equal(controls.submit.disabled,false,"Validation retry retains server-posted hidden token");
 for(const invalid of ["short","A".repeat(44),"A".repeat(42)+"%", "<script>"+"A".repeat(35)]){
  controls=reset("#"+invalid);assert.equal(controls.token.value,"");assert.equal(controls.submit.disabled,true);assert.equal(replaced,"/reset-password");
 }
 controls=reset("#"+secret);controls.token.value="changed";prevented=false;
 form.submit({preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(controls.submit.disabled,true);
 form=null;preparePasswordReset();
}
'''
        result = subprocess.run([nodePath, "-e", harness + function + "\ncheck().catch(error=>{console.error(error);process.exitCode=1;});"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
