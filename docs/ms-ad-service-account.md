# Active Directory Service Account Setup

This guide explains how to create a dedicated, least-privilege service account in **Microsoft Active Directory** for
Akvatar's LDAP integration. The account will only have permission to **read user objects** and **write photo attributes
** (e.g. `thumbnailPhoto`, `jpegPhoto`) within a specific Organizational Unit (OU).

## Overview

Akvatar connects to LDAP (Active Directory) to write profile photos directly into user objects. To follow the principle
of least privilege, the service account should:

- Be able to **bind** (authenticate) to the directory
- **Search** for user objects within a specific OU
- **Read** the `objectSid` and `distinguishedName` attributes of matched users (`objectSid` is required to use it as a
  search filter)
- **Write** only the photo attributes configured in `ldap.photos` (e.g. `thumbnailPhoto`, `jpegPhoto`)
- Have **no other permissions** (no password reset, no group membership changes, no account creation)

## What the application does with LDAP

1. **Bind** as the service account using the configured `bind_dn` and `bind_password`
2. **Search** under `search_base` using `search_filter` (default: `(objectSid={ldap_uniq})`) to find the user
3. **Read** the `distinguishedName` of the matched user
4. **Modify** all configured photo attributes (e.g. `thumbnailPhoto`, `jpegPhoto`) in a single operation
5. **Unbind** (disconnect)

Step 3 (reading `distinguishedName`) is typically allowed for all authenticated users in AD. The critical permission is
step 4: writing `thumbnailPhoto`.

## PowerShell setup script

The script below automates the complete setup using PowerShell and .NET `System.DirectoryServices` classes. It:

1. Creates a service account in a dedicated OU
2. Generates a random 32-character password
3. Delegates **Read Property** on `objectSid` and **Read/Write Property** on `thumbnailPhoto` to the service account,
   scoped to user objects within the target OU

### Prerequisites

- Run from a machine with the **Active Directory PowerShell module** (`RSAT-AD-PowerShell`)
- Run as a user with **Domain Admin** or equivalent permissions
- Adjust the variables in the configuration section to match your environment

### Script

```powershell
# Import module
Import-Module ActiveDirectory

# ============================================================================
# Configuration - adjust these values to match your environment
# ============================================================================

# Domain components (e.g. DC=corp,DC=example,DC=com for corp.example.com)
$DomainDN = "DC=corp,DC=example,DC=com"

# OU where the service account will be created
# (this OU will be created if it does not exist)
$ServiceAccountOU = "OU=Service Accounts,$DomainDN"

# Service account name
$SamAccountName = "sa-akvatar"
$DisplayName    = "Akvatar Service Account"
$Description    = "Least-privilege service account for the Akvatar LDAP integration. Allowed to write photo attributes on user objects."

# Target OU where user objects live (the service account will be granted
# write access to thumbnailPhoto only on user objects within this OU)
$TargetUsersOU = "OU=Users,$DomainDN"

# LDAP attributes the application writes to (must match ldap.photos entries
# in config.yml).  Add all attributes configured in your ldap.photos array.
$PhotoAttributes = @("thumbnailPhoto", "jpegPhoto")

# Set to $true to preview all actions without making any changes to Active Directory.
$DryRun = $false

# Derive the domain FQDN from the DC components of $DomainDN
# (e.g. "DC=corp,DC=example,DC=com" -> "corp.example.com") -- used for UserPrincipalName.
$DomainFQDN = ($DomainDN -split "," | Where-Object { $_ -match "^DC=" } | ForEach-Object { $_ -replace "^DC=", "" }) -join "."

# ============================================================================
# Step 1: Generate a random 32-character password
# ============================================================================
# Uses .NET RNGCryptoServiceProvider for cryptographically secure randomness.
# Character set includes uppercase, lowercase, digits, and symbols.

$PasswordLength = 32
$CharacterSet   = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%&*()-_=+[]{}|;:,.<>?"

# Generate random bytes and map them to the character set
$RNG = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
$Bytes = [byte[]]::new($PasswordLength)
$RNG.GetBytes($Bytes)
$Password = -join ($Bytes | ForEach-Object { $CharacterSet[$_ % $CharacterSet.Length] })
$RNG.Dispose()

$SecurePassword = ConvertTo-SecureString -String $Password -AsPlainText -Force

Write-Host "Generated password: $Password" -ForegroundColor Yellow
Write-Host "Store this password securely - it will not be shown again." -ForegroundColor Yellow
Write-Host ""

# ============================================================================
# Step 2: Create the Service Account OU (if it does not exist)
# ============================================================================

$OUExists = $null
try { $OUExists = Get-ADOrganizationalUnit -Identity $ServiceAccountOU } catch {}

if (-not $OUExists) {
    # Extract the OU name from the DN (first OU= component)
    $OUName = ($ServiceAccountOU -split ",")[0] -replace "^OU=", ""
    $ParentDN = ($ServiceAccountOU -split ",", 2)[1]

    if ($DryRun) {
        Write-Host "[DRY RUN] Would create OU: $ServiceAccountOU" -ForegroundColor DarkYellow
    } else {
        New-ADOrganizationalUnit `
            -Name $OUName `
            -Path $ParentDN `
            -Description "Service accounts for automated systems" `
            -ProtectedFromAccidentalDeletion $true

        Write-Host "Created OU: $ServiceAccountOU" -ForegroundColor Green
    }
} else {
    Write-Host "OU already exists: $ServiceAccountOU" -ForegroundColor Cyan
}

# ============================================================================
# Step 3: Create the service account
# ============================================================================

$AccountExists = $null
try { $AccountExists = Get-ADUser -Identity $SamAccountName } catch {}

if ($AccountExists) {
    Write-Host "Service account '$SamAccountName' already exists - skipping creation." -ForegroundColor Cyan
} else {
    if ($DryRun) {
        Write-Host "[DRY RUN] Would create service account: $SamAccountName (UPN: $SamAccountName@$DomainFQDN)" -ForegroundColor DarkYellow
    } else {
        New-ADUser `
            -SamAccountName $SamAccountName `
            -UserPrincipalName "$SamAccountName@$DomainFQDN" `
            -Name $DisplayName `
            -DisplayName $DisplayName `
            -Description $Description `
            -Path $ServiceAccountOU `
            -AccountPassword $SecurePassword `
            -Enabled $true `
            -PasswordNeverExpires $true `
            -CannotChangePassword $true `
            -ChangePasswordAtLogon $false

        Write-Host "Created service account: $SamAccountName" -ForegroundColor Green
    }
}

# Retrieve the account's SID (needed for ACL delegation)
# In dry run mode the account may not exist yet; skip SID lookup and ACL steps.
if ($DryRun -and -not $AccountExists) {
    Write-Host "[DRY RUN] Skipping SID lookup and ACL delegation (account not yet created)" -ForegroundColor DarkYellow
    Write-Host ""
    Write-Host "[DRY RUN] No changes were made to Active Directory." -ForegroundColor DarkYellow
    exit 0
}

$ServiceAccount = Get-ADUser -Identity $SamAccountName
$ServiceAccountSID = [System.Security.Principal.SecurityIdentifier]$ServiceAccount.SID

Write-Host "Service account SID: $ServiceAccountSID" -ForegroundColor Cyan
Write-Host "Service account DN:  $($ServiceAccount.DistinguishedName)" -ForegroundColor Cyan
Write-Host ""

# ============================================================================
# Step 4: Look up the schemaIDGUID for the photo attribute and user class
# ============================================================================
# Every AD attribute and class has a unique GUID in the schema partition.
# We look these up dynamically so the script works in any AD forest.

$RootDSE   = [System.DirectoryServices.DirectoryEntry]::new("LDAP://RootDSE")
$SchemaDN  = $RootDSE.Properties["schemaNamingContext"].Value

# --- Photo attribute GUIDs (e.g. thumbnailPhoto, jpegPhoto) ---
$PhotoAttrGUIDs = @{}
foreach ($Attr in $PhotoAttributes) {
    $AttrSearcher = [System.DirectoryServices.DirectorySearcher]::new(
        [System.DirectoryServices.DirectoryEntry]::new("LDAP://$SchemaDN"),
        "(ldapDisplayName=$Attr)",
        @("schemaIDGUID"),
        [System.DirectoryServices.SearchScope]::Subtree
    )
    $AttrResult = $AttrSearcher.FindOne()

    if (-not $AttrResult) {
        Write-Error "Could not find attribute '$Attr' in the AD schema."
        exit 1
    }

    $PhotoAttrGUIDs[$Attr] = [Guid]$AttrResult.Properties["schemaidguid"][0]
    Write-Host "Schema GUID for '$Attr': $($PhotoAttrGUIDs[$Attr])" -ForegroundColor Cyan
}

# --- objectSid attribute GUID ---
# Required so the service account can use objectSid as a search filter value.
$SidSearcher = [System.DirectoryServices.DirectorySearcher]::new(
    [System.DirectoryServices.DirectoryEntry]::new("LDAP://$SchemaDN"),
    "(ldapDisplayName=objectSid)",
    @("schemaIDGUID"),
    [System.DirectoryServices.SearchScope]::Subtree
)
$SidResult = $SidSearcher.FindOne()

if (-not $SidResult) {
    Write-Error "Could not find attribute 'objectSid' in the AD schema."
    exit 1
}

$ObjectSidGUID = [Guid]$SidResult.Properties["schemaidguid"][0]
Write-Host "Schema GUID for 'objectSid':     $ObjectSidGUID" -ForegroundColor Cyan

# --- User object class GUID ---
$ClassSearcher = [System.DirectoryServices.DirectorySearcher]::new(
    [System.DirectoryServices.DirectoryEntry]::new("LDAP://$SchemaDN"),
    "(&(objectClass=classSchema)(ldapDisplayName=user))",
    @("schemaIDGUID"),
    [System.DirectoryServices.SearchScope]::Subtree
)
$ClassResult = $ClassSearcher.FindOne()

if (-not $ClassResult) {
    Write-Error "Could not find 'user' class in the AD schema."
    exit 1
}

$UserClassGUID = [Guid]$ClassResult.Properties["schemaidguid"][0]
Write-Host "Schema GUID for 'user' class:     $UserClassGUID" -ForegroundColor Cyan
Write-Host ""

# ============================================================================
# Step 5: Delegate Write Property on all photo attributes
# ============================================================================
# Grant the service account the ability to write (modify) each configured
# photo attribute on user objects within the target OU.
#
# InheritanceType = Descendents: the ACE applies to child objects, not the OU itself
# InheritedObjectType = user class GUID: only applies to objects of type "user"
# ObjectType = photo attribute GUID: limits the permission to that single attribute

$TargetOU_DE = [System.DirectoryServices.DirectoryEntry]::new("LDAP://$TargetUsersOU")
$ACL = $TargetOU_DE.ObjectSecurity

# --- ACEs: Write + Read Property on each photo attribute for user objects ---
foreach ($Attr in $PhotoAttributes) {
    $AttrGUID = $PhotoAttrGUIDs[$Attr]

    # Write Property
    $WriteACE = [System.DirectoryServices.ActiveDirectoryAccessRule]::new(
        [System.Security.Principal.IdentityReference]$ServiceAccountSID,  # Who
        [System.DirectoryServices.ActiveDirectoryRights]::WriteProperty,  # What
        [System.Security.AccessControl.AccessControlType]::Allow,         # Allow/Deny
        $AttrGUID,                                                        # Which attribute
        [System.DirectoryServices.ActiveDirectorySecurityInheritance]::Descendents, # Where
        $UserClassGUID                                                    # On which object type
    )
    $ACL.AddAccessRule($WriteACE)

    # Read Property (allows the service account to verify the current value)
    $ReadACE = [System.DirectoryServices.ActiveDirectoryAccessRule]::new(
        [System.Security.Principal.IdentityReference]$ServiceAccountSID,
        [System.DirectoryServices.ActiveDirectoryRights]::ReadProperty,
        [System.Security.AccessControl.AccessControlType]::Allow,
        $AttrGUID,
        [System.DirectoryServices.ActiveDirectorySecurityInheritance]::Descendents,
        $UserClassGUID
    )
    $ACL.AddAccessRule($ReadACE)

    Write-Host "  Delegated Read/Write Property on '$Attr'" -ForegroundColor Green
}

# --- ACE: Read Property on objectSid for user objects ---
# The default search filter (objectSid={ldap_uniq}) requires ReadProperty on
# objectSid. Without this, AD treats the attribute as invisible to the account
# and the filter returns no results.
$ReadSidACE = [System.DirectoryServices.ActiveDirectoryAccessRule]::new(
    [System.Security.Principal.IdentityReference]$ServiceAccountSID,
    [System.DirectoryServices.ActiveDirectoryRights]::ReadProperty,
    [System.Security.AccessControl.AccessControlType]::Allow,
    $ObjectSidGUID,
    [System.DirectoryServices.ActiveDirectorySecurityInheritance]::Descendents,
    $UserClassGUID
)
$ACL.AddAccessRule($ReadSidACE)

# Commit the ACL changes to Active Directory
if ($DryRun) {
    Write-Host "[DRY RUN] Would commit Read/Write Property ACEs for photo attributes" -ForegroundColor DarkYellow
} else {
    $TargetOU_DE.ObjectSecurity = $ACL
    $TargetOU_DE.CommitChanges()
}

Write-Host "Delegated Read/Write Property on photo attributes to '$SamAccountName'" -ForegroundColor Green
Write-Host "Delegated Read Property on 'objectSid' to '$SamAccountName'" -ForegroundColor Green
Write-Host "  Scope: user objects under $TargetUsersOU" -ForegroundColor Green
Write-Host ""

# ============================================================================
# Step 6: Grant generic read access on the target OU
# ============================================================================
# The service account needs to search for user objects (to find the DN by
# objectSid or other filter).  This grants GenericRead on user objects within
# the target OU.  In many AD environments Authenticated Users already have
# this, but granting it explicitly ensures the account works even if default
# permissions have been tightened.

$GenericReadACE = [System.DirectoryServices.ActiveDirectoryAccessRule]::new(
    [System.Security.Principal.IdentityReference]$ServiceAccountSID,
    [System.DirectoryServices.ActiveDirectoryRights]::GenericRead,
    [System.Security.AccessControl.AccessControlType]::Allow,
    [System.DirectoryServices.ActiveDirectorySecurityInheritance]::Descendents,
    $UserClassGUID
)
$ACL.AddAccessRule($GenericReadACE)

if ($DryRun) {
    Write-Host "[DRY RUN] Would commit GenericRead ACE for user objects" -ForegroundColor DarkYellow
} else {
    $TargetOU_DE.ObjectSecurity = $ACL
    $TargetOU_DE.CommitChanges()
}
$TargetOU_DE.Dispose()

Write-Host "Delegated GenericRead on user objects to '$SamAccountName'" -ForegroundColor Green
Write-Host "  Scope: user objects under $TargetUsersOU" -ForegroundColor Green
Write-Host ""

# ============================================================================
# Summary
# ============================================================================

Write-Host "========================================" -ForegroundColor White
Write-Host " Setup complete" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor White
Write-Host ""
Write-Host "Service account:  $SamAccountName" -ForegroundColor White
Write-Host "Distinguished DN: $($ServiceAccount.DistinguishedName)" -ForegroundColor White
Write-Host "Password:         (see above - store securely)" -ForegroundColor White
Write-Host ""
Write-Host "Permissions granted on: $TargetUsersOU" -ForegroundColor White
Write-Host "  - GenericRead on user objects (search and read all attributes)" -ForegroundColor White
foreach ($Attr in $PhotoAttributes) {
    Write-Host "  - Read/Write Property on '$Attr' on user objects" -ForegroundColor White
}
Write-Host "  - Read Property on 'objectSid' on user objects" -ForegroundColor White
Write-Host ""
Write-Host "Use the following values in config.yml:" -ForegroundColor Yellow
Write-Host ""
Write-Host "ldap:" -ForegroundColor Yellow
Write-Host "  bind_dn: `"$($ServiceAccount.DistinguishedName)`"" -ForegroundColor Yellow
Write-Host "  bind_password: `"<the generated password>`"" -ForegroundColor Yellow
Write-Host "  search_base: `"$TargetUsersOU`"" -ForegroundColor Yellow
Write-Host "  search_filter: `"(objectSid={ldap_uniq})`"" -ForegroundColor Yellow
```

## What the script does

| Step | Action                                                                                                                                                |
|------|-------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1    | Generates a cryptographically random 32-character password                                                                                            |
| 2    | Creates the Service Accounts OU (if it does not exist)                                                                                                |
| 3    | Creates the service account with password-never-expires and cannot-change-password flags                                                              |
| 4    | Looks up the `schemaIDGUID` for each configured photo attribute, `objectSid`, and the `user` object class from the AD schema                          |
| 5    | Delegates **Write Property** and **Read Property** on each photo attribute, and **Read Property** on `objectSid` for user objects under the target OU |
| 6    | Delegates **GenericRead** on user objects under the target OU so the account can search and read all attributes                                       |

## Understanding the permissions

### Why Write Property (not GenericWrite)?

`GenericWrite` includes Write Property, Write Validated, and Self membership changes. The service account only needs to
replace specific attribute values, so `WriteProperty` scoped to each individual photo attribute is the minimal
permission.

### Why scope to user objects?

The `InheritedObjectType` parameter limits the ACE to objects of the `user` class. Computer objects, groups, and other
directory objects within the same OU are not affected.

### Why Descendents inheritance?

The `Descendents` inheritance type means the permission applies to child objects of the target OU, not to the OU object
itself. This prevents the service account from modifying the OU's own attributes.

## Verifying the permissions

After running the script, verify the delegation using either of these methods.

### Method 1: PowerShell

```powershell
# List all ACEs for the service account on the target OU
$OU = [System.DirectoryServices.DirectoryEntry]::new("LDAP://OU=Users,DC=corp,DC=example,DC=com")
$ACL = $OU.ObjectSecurity

$ACL.Access | Where-Object {
    $_.IdentityReference -match "sa-akvatar"
} | Format-Table ActiveDirectoryRights, ObjectType, InheritedObjectType, InheritanceType -AutoSize
```

Expected output:

```
ActiveDirectoryRights ObjectType                           InheritedObjectType                  InheritanceType
--------------------- ----------                           -------------------                  ---------------
        WriteProperty 8d3bca50-1d7e-11d0-a081-00aa006c33ed bf967aba-0de6-11d0-a285-00aa003049e2    Descendents
         ReadProperty 8d3bca50-1d7e-11d0-a081-00aa006c33ed bf967aba-0de6-11d0-a285-00aa003049e2    Descendents
        WriteProperty 9c979768-ba1a-4c08-9632-c6a5c1ed649a bf967aba-0de6-11d0-a285-00aa003049e2    Descendents
         ReadProperty 9c979768-ba1a-4c08-9632-c6a5c1ed649a bf967aba-0de6-11d0-a285-00aa003049e2    Descendents
         ReadProperty bf96798f-0de6-11d0-a285-00aa003049e2 bf967aba-0de6-11d0-a285-00aa003049e2    Descendents
          GenericRead 00000000-0000-0000-0000-000000000000 bf967aba-0de6-11d0-a285-00aa003049e2    Descendents
```

> The first pair of ObjectType GUIDs corresponds to `thumbnailPhoto`, the second to `jpegPhoto`. Actual GUIDs may differ
> between AD forests.

### Method 2: Active Directory Users and Computers (GUI)

1. Open **Active Directory Users and Computers**
2. Enable **View > Advanced Features**
3. Right-click the target OU (e.g. `Users`) and select **Properties > Security**
4. Click **Advanced**
5. Look for entries with `sa-akvatar` as the principal
6. Verify the permissions match: Write/Read on each configured photo attribute on User objects

## Testing the service account

Test the connection and permissions before deploying the application:

```powershell
# Quick test: bind as the service account and search for a user
$Cred = Get-Credential -UserName "sa-akvatar"

Get-ADUser -Filter "objectSid -eq 'S-1-5-21-XXXXXXXXXX-XXXXXXXXXX-XXXXXXXXXX-XXXX'" `
    -Server "dc.corp.example.com" `
    -Credential $Cred `
    -Properties distinguishedName, thumbnailPhoto, jpegPhoto |
    Select-Object distinguishedName, @{N="HasThumbnail"; E={$null -ne $_.thumbnailPhoto}}, @{N="HasJpegPhoto"; E={$null -ne $_.jpegPhoto}}
```

Replace the `objectSid` with a real user's SID from your environment.

## Config.yml reference

After running the setup script, fill in the LDAP section of `config.yml`:

```yaml
ldap:
  enabled: true
  servers: "ldaps://dc.corp.example.com"
  port: 636
  use_ssl: true
  skip_cert_verify: false
  bind_dn: "CN=Akvatar Service Account,OU=Service Accounts,DC=corp,DC=example,DC=com"
  bind_password: "<the generated password>"
  search_base: "OU=Users,DC=corp,DC=example,DC=com"
  search_filter: "(objectSid={ldap_uniq})"
  photos:
    - attribute: thumbnailPhoto
      type: binary
      image_type: jpeg
      image_size: 96
      max_file_size: 100
    - attribute: jpegPhoto
      type: binary
      image_type: jpeg
      image_size: 648
      max_file_size: 0
```

## Removing the service account

To clean up the service account and its delegated permissions:

```powershell
# Remove delegated ACEs from the target OU
$OU = [System.DirectoryServices.DirectoryEntry]::new("LDAP://OU=Users,DC=corp,DC=example,DC=com")
$ACL = $OU.ObjectSecurity
$Account = Get-ADUser -Identity "sa-akvatar"
$SID = [System.Security.Principal.SecurityIdentifier]$Account.SID

# Remove all ACEs for this account
$ACL.Access | Where-Object {
    $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]) -eq $SID
} | ForEach-Object {
    $ACL.RemoveAccessRule($_) | Out-Null
}

$OU.ObjectSecurity = $ACL
$OU.CommitChanges()
$OU.Dispose()

# Delete the service account
Remove-ADUser -Identity "sa-akvatar" -Confirm:$false

Write-Host "Service account and delegated permissions removed." -ForegroundColor Green
```
