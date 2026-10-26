import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";

const data = JSON.parse(document.getElementById("auth-data").textContent);
for (const name of ["client", "resource"]) document.getElementById(name).textContent = data[name === "client" ? "clientId" : name];
for (const permission of data.scopeLabels) {
  const item = document.createElement("li");
  item.textContent = `${permission.label} (${permission.scope})`;
  document.getElementById("scope-list").appendChild(item);
}
let idToken = null;
const status = document.getElementById("status");
document.getElementById("signin").onclick = async () => {
  try {
    const result = await signInWithPopup(getAuth(initializeApp(data.firebase)), new GoogleAuthProvider());
    idToken = await result.user.getIdToken();
    document.getElementById("allow").disabled = false;
    status.textContent = "Signed in. Review the request, then choose Allow or Deny.";
  } catch (_) { status.textContent = "Google sign-in did not complete."; }
};
function decide(action) {
  const form = document.getElementById("decision");
  document.getElementById("csrf").value = data.csrf;
  document.getElementById("firebase-token").value = action === "allow" ? idToken : "";
  form.action = `/authorize/${encodeURIComponent(data.requestId)}/${action}`;
  form.submit();
}
document.getElementById("allow").onclick = () => decide("allow");
document.getElementById("deny").onclick = () => decide("deny");
