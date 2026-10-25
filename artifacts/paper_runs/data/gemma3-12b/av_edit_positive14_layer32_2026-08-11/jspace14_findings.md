# J-space analysis of the Gemma layer-32 AV-edit sweep

## Scope

The existing Gemma layer-32 Jacobian lens was applied to all 28 prompt-specific
vectors at rank 32. The analysis includes prompt-pair consistency, centroid
geometry, token-level Jacobian readouts at alpha 0.01 and 0.26, and
magnitude-preserving full/projection/complement ablations for the three clean
fixed-alpha concepts (11, 287, and 359).

## Geometry

Across all 28 prompt vectors:

- mean rank-32 projection fraction: 0.0262;
- mean Jacobian alignment: 0.01185;
- mean within-concept prompt-pair cosine: 0.1969.

The three clean concepts have much more consistent prompt vectors than the
other eleven concepts (mean pair cosine 0.5588 versus 0.0982). Their mean
centroid projection fraction is not larger (0.0279 versus 0.0341), so success
is associated with prompt consistency rather than unusually strong membership
in this rank-32 J-space.

## Token readout

The lens moderately predicts the one-token logit shift:

| Alpha | Mean predicted/actual cosine | Mean top-50 overlap |
|---:|---:|---:|
| 0.01 | 0.585 | 6.33 |
| 0.26 | 0.552 | 4.81 |

For the clean concepts at alpha 0.01, the best actual target-token ranks are 9
(c11), 6 (c287), and 2 (c359). This confirms that their vectors immediately
raise target-related logits, although the rank-32 transport is only a moderate
approximation of the full nonlinear effect.

## Magnitude-preserving ablation

Each centroid was decomposed into its rank-32 projection and orthogonal
complement. Component norms were preserved when setting the effective alpha.

| Concept | Projection norm | Full hit | Projection hit | Complement hit |
|---:|---:|---:|---:|---:|
| 11 | 0.0313 | 100% | 0% | 100% |
| 287 | 0.0211 | 100% | 0% | 100% |
| 359 | 0.0314 | 100% | 0% | 100% |

The rank-32 projection is not sufficient to reproduce steering at its natural
magnitude. The complement retains the complete measured effect for all three
concepts. Thus, the successful Gemma steering directions lie overwhelmingly
outside the lens's top-32 locally output-sensitive subspace.

This does not mean J-space is irrelevant: normalizing the tiny projection would
ask a different intervention question and artificially amplify it. The result
specifically establishes that the naturally scaled rank-32 component does not
carry the observed behavioral effect.
