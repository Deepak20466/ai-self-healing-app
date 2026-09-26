// In-memory "database" of users.
const USERS = {
  1: { id: 1, name: "ada", plan: "pro" },
  2: { id: 2, name: "grace", plan: "free" },
};

function findUser(id) {
  return USERS[id]; // undefined for an unknown id
}

// SEEDED BUG: findUser() returns undefined for an unknown id, but this
// dereferences the result without checking -> TypeError for /users/999.
function displayName(id) {
  const user = findUser(id);
  return user.name.toUpperCase();
}

module.exports = { findUser, displayName };
