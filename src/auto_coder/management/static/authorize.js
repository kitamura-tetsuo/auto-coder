import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";

const data = JSON.parse(document.getElementById("auth-data").textContent);
for (const name of ["client", "resource", "scope"]) document.getElementById(name).textContent = data[name === "client" ? "clientId" : name];
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
async function decide(action) {
  const response = await fetch(`/authorize/${encodeURIComponent(data.requestId)}/${action}`, {
    method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({csrf: data.csrf, firebaseIdToken: action === "allow" ? idToken : undefined}),
  });
  if (response.redirected) window.location.assign(response.url);
  else status.textContent = response.ok ? "Completed." : "Authorization failed.";
}
document.getElementById("allow").onclick = () => decide("allow");
document.getElementById("deny").onclick = () => decide("deny");
