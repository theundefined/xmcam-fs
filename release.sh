#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Configuration
PROJECT_NAME="xmcam-fs"
DEFAULT_INITIAL_VERSION="v0.1.0"
VERSION_BUMP_TYPE="patch" # Can be major, minor, patch

# Function to display usage
usage() {
  echo "Usage: $0 [major|minor|patch|vX.Y.Z]"
  echo "  Automatically bumps the version, creates a git tag, and pushes it."
  echo "  If a version is specified (vX.Y.Z), it will use that exact version."
  exit 1
}

# Parse arguments
if [ "$#" -eq 1 ]; then
  if [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    NEW_VERSION_ARG="$1"
  elif [[ "$1" =~ ^(major|minor|patch)$ ]]; then
    VERSION_BUMP_TYPE="$1"
  else
    usage
  fi
elif [ "$#" -ne 0 ]; then
  usage
fi

# Fetch all tags to ensure we have the latest
echo "Fetching latest tags..."
git fetch origin --tags

# Get the latest version tag
LAST_TAG=$(git describe --tags --abbrev=0 --match "v[0-9]*.[0-9]*.[0-9]*" 2>/dev/null || echo "$DEFAULT_INITIAL_VERSION")

# If no semantic version tags exist, start with the default initial version and assume 0.0.0 for first bump
if [ "$LAST_TAG" = "$DEFAULT_INITIAL_VERSION" ] && [ "$(git tag --list "$DEFAULT_INITIAL_VERSION")" = "" ]; then
    echo "No semantic version tags found. Starting with $DEFAULT_INITIAL_VERSION."
    CURRENT_VERSION_SEMVER="0.0.0" # Base for first bump
else
    CURRENT_VERSION_SEMVER=$(echo "$LAST_TAG" | sed 's/^v//')
fi

# Determine the new version
if [ -n "$NEW_VERSION_ARG" ]; then
    NEW_VERSION=$(echo "$NEW_VERSION_ARG" | sed 's/^v//')
    echo "Using specified version: v$NEW_VERSION"
else
    # Split version into major, minor, patch
    IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION_SEMVER"

    case "$VERSION_BUMP_TYPE" in
        major)
            MAJOR=$((MAJOR + 1))
            MINOR=0
            PATCH=0
            ;;
        minor)
            MINOR=$((MINOR + 1))
            PATCH=0
            ;;
        patch)
            PATCH=$((PATCH + 1))
            ;;
        *)
            echo "Invalid bump type: $VERSION_BUMP_TYPE. Must be major, minor, or patch."
            usage
            ;;
    esac
    NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
    echo "Bumping $VERSION_BUMP_TYPE version: v$CURRENT_VERSION_SEMVER -> v$NEW_VERSION"
fi

# Update version in pyproject.toml
sed -i "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml

# Create commit for version bump only if there are changes
git add pyproject.toml
if ! git diff --cached --quiet; then
    git commit -m "Bump version to v$NEW_VERSION"
    echo "Committed version bump."
else
    echo "Version in pyproject.toml is already up to date. Skipping commit."
fi

# Create new tag if it doesn't exist
NEW_TAG="v$NEW_VERSION"
if git rev-parse "$NEW_TAG" >/dev/null 2>&1; then
    echo "Tag $NEW_TAG already exists."
else
    echo "Creating git tag: $NEW_TAG"
    git tag "$NEW_TAG" -m "$PROJECT_NAME Release $NEW_VERSION"
fi

# Push commit and tag
echo "Pushing changes and tag to origin..."
git push origin main || echo "Main already up to date or push failed (skipping)"
git push origin "$NEW_TAG"

# Get repository owner and name
REPO_URL=$(git config --get remote.origin.url)
REPO_OWNER=$(echo "$REPO_URL" | sed -E 's/.*github.com[:/]([^/]+)\/.*/\1/')
REPO_NAME=$(echo "$REPO_URL" | sed -E 's/.*github.com[:/][^/]+\/([^/\. ]+)(\.git)?/\1/')
REPO_PATH="$REPO_OWNER/$REPO_NAME"

# Construct Actions URL
ACTIONS_URL="https://github.com/$REPO_PATH/actions/workflows/ci.yml"
echo "Monitor action status here: $ACTIONS_URL"

echo "Release process initiated with tag $NEW_TAG. Check GitHub Actions for build status."
