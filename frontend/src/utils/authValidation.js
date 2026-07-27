export const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function getPasswordChecks(password) {
  return {
    length: password.length >= 8,
    uppercase: /[A-Z]/.test(password),
    lowercase: /[a-z]/.test(password),
    number: /\d/.test(password),
  }
}

export function isStrongPassword(password) {
  return Object.values(getPasswordChecks(password)).every(Boolean)
}
